#!/usr/bin/env python
# **************************************************************************
# *
# * Authors:     J.M. de la Rosa Trevin (delarosatrevin@gmail.com)
# *
# * This program is free software; you can redistribute it and/or modify
# * it under the terms of the GNU General Public License as published by
# * the Free Software Foundation; either version 3 of the License, or
# * (at your option) any later version.
# *
# * This program is distributed in the hope that it will be useful,
# * but WITHOUT ANY WARRANTY; without even the implied warranty of
# * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# * GNU General Public License for more details.
# *
# **************************************************************************

""" 
JSON server that will be connected to EMhub and will manage
the sessions' folders creation and data transfer.
"""
import os
import sys
import time
import argparse
import threading
from datetime import datetime, timedelta
from glob import glob
from collections import OrderedDict
import configparser
from pprint import pprint
from apscheduler.schedulers.background import BackgroundScheduler
import traceback

from emtools.utils import Pretty, Process, JsonTCPServer, JsonTCPClient, Path, Color
from emtools.metadata import EPU

from emhub.client import open_client, config


def __server_address():
    parts = os.environ.get('SESSIONS_SERVER_ADDRESS', 'localhost:5555').split(':')
    return parts[0], int(parts[1])


SESSIONS_SERVER_ADDRESS = __server_address()
SESSIONS_DATA_FOLDER = os.environ.get('SESSIONS_DATA_FOLDER', None)


class SessionsServer(JsonTCPServer):
    def __init__(self, address=None):
        address = address or SESSIONS_SERVER_ADDRESS
        JsonTCPServer.__init__(self, address)
        print(Color.green(f"Connected to server: {config.EMHUB_SERVER_URL}"))
        self._refresh = 30
        self.init_data()
        self._files = {}
        self._scheduler = None

    def init_data(self):
        print("- Loading sessions...")
        self.sessions = self.request_dict('get_sessions')
        print(f"    Total: {len(self.sessions)}")

        print("- Loading resources...")
        self.resources = self.request_dict('get_resources',
                                           {"attrs": ["id", "name"]})
        print(f"    Total: {len(self.resources)}")

        print("- Loading config...")
        self.config = self.request_config('sessions')

        def _check_folder(key, folder):
            exists = folder and os.path.exists(folder)
            color = Color.bold if exists else Color.red
            print(f"{key}: {color(folder)}")
            if not exists:
                print(Color.red(">>> Missing folder!!! Exiting."))
                sys.exit(1)

        _check_folder("SESSIONS_DATA_FOLDER", SESSIONS_DATA_FOLDER)
        for k, v in self.get_folders().items():
            _check_folder(k, v[1])

        if not os.path.exists(SESSIONS_DATA_FOLDER):
            print(Color.red(" Missing"))

    def request_data(self, endpoint, jsonData=None):
        with open_client() as dc:
            return dc.request(endpoint, jsonData=jsonData).json()

    def request_dict(self, endpoint, jsonData=None):
        return {s['id']: s for s in self.request_data(endpoint, jsonData=jsonData)}

    def request_config(self, config):
        data = {'attrs': {'config': config}}
        return self.request_data('get_config', jsonData=data)['config']

    def print(self, *args):
        if self.verbose:
            print(*args)

    def offload_session_files(self, session):
        """ Move files from the Raw folder to the Offload folder.
        Files will be moved when there has been a time without modification
        (file's timeout).
        """
        src = session['raw']['path']
        dst = session['offload']['path']
        ed = Path.ExtDict()

        now = datetime.now()
        td = timedelta(minutes=1)

        def _updateSessionFiles():
            new_session = dict(session)
            new_session['updated'] = Pretty.now()
            raw = new_session['raw']
            if 'files' in raw:
                ed.update(raw['files'])
            raw['files'] = dict(ed)
            ed.clear()
            self.update([new_session])

        def _moveFile(srcFile, dstFile):
            s = os.stat(srcFile)
            dt = datetime.fromtimestamp(s.st_mtime)
            if now - dt >= td:
                Process.system(f'rsync -ac --remove-source-files {srcFile} {dstFile}')
                ed.register(srcFile, stat=s)
                if len(ed) >= 10:  # update session info more frequently
                    _updateSessionFiles()

        Path.copyDir(src, dst, copyFileFunc=_moveFile)
        if ed:
            _updateSessionFiles()

    def update_active_sessions(self):
        for s in self.active_sessions():
            print(f"   Active session: {s['name']}, checking files")
            self.offload_session_files(s)

    def active_otf(self, session):
        otf = session['extra'].get('otf', {})
        status = otf.get('status', None)
        if status not in ['running', 'launched', 'created']:
            return False
        otf_path = otf.get('path', '')

        if otf and os.path.exists(otf_path):
            fn, last_modified = Path.lastModified(otf_path)
            now = datetime.now()
            print(f"OTF: Running, last: {fn}, {last_modified}")
            if (now - last_modified).days >= 1:
                return False
        return True

    def get_folders(self):
        folders = OrderedDict()
        for f in ['EPU', 'Offload', 'OTF', 'Groups']:
            folder = os.path.join(SESSIONS_DATA_FOLDER, f)
            fpath = os.path.abspath(os.path.realpath(folder))
            folders[f] = (folder, fpath)
        return folders

    def create_session_otf(self, session):
        # Get absolute OTF root folder
        otf_root = self.get_folders()['OTF'][1]
        extra = session['extra']
        raw_path = extra['raw']['path']

        print(f">>> Creating OTF session from: {raw_path}")
        if not os.path.exists(raw_path):
            raise Exception("Input folder does not exists")

        attrs = {"attrs": {"id": session['id']}}
        users = self.request_data('get_session_users', attrs)['session_users']
        date_ts = Pretty.now()  # Fixme Maybe use first file creation (for old sessions)
        date = date_ts.split()[0].replace('-', '')
        microscope = self.resources[session['resource_id']]['name']

        name = session['name']
        otf_folder = f"{date}_{microscope}_{name}_OTF"
        otf_path = os.path.join(otf_root, otf_folder)
        extra['otf'] = {'path': otf_path, 'status': 'created'}
        session['data_path'] = otf_path
        print(f"rm -rf {otf_path}")
        os.system(f"rm -rf {otf_path}")

        def _path(*paths):
            return os.path.join(otf_path, *paths)

        os.mkdir(otf_path)
        os.symlink(raw_path, _path('data'))

        gain_pattern = self.config['data']['gain']
        possible_gains = glob(gain_pattern.format(microscope=microscope))
        if possible_gains:
            gain = possible_gains[0]
            os.symlink(os.path.realpath(gain), _path('gain.mrc'))

        # Create a general ini file with config/information of the session
        config = configparser.ConfigParser()

        operator = users['operator'].get('name', 'No-operator')
        config['GENERAL'] = {
            'group': users['group'],
            'user': users['owner']['name'],
            'operator': operator,
            'microscope': microscope,
            'raw_data': raw_path
        }

        acq = dict(self.config['acquisition'][microscope])
        config['ACQUISITION'] = acq

        config['PREPROCESSING'] = {
            'images': 'data/Images-Disc1/GridSquare_*/Data/Foil*fractions.tiff',
            'software': 'None',  # or Relion or Scipion
        }

        with open(_path('README.txt'), 'w') as configfile:
            config.write(configfile)

        sconfig = self.request_config('sessions')
        opts = sconfig['otf']['relion']['common']
        with open(_path('relion_it_options.py'), 'w') as f:
            optStr = ",\n".join(f"'{k}' : '{v.format(**acq)}'" for k, v in opts.items())
            f.write("{\n%s\n}\n" % optStr)

    def launch_sessions_otf(self, session):
        microscope = self.resources[session['resource_id']]['name']
        otf = session['extra']['otf']
        otf_path = otf['path']
        sconfig = self.request_config('sessions')
        command = sconfig['otf']['command']
        cmd = command[microscope].format(otf_path=otf_path,
                                         session_id=session['id'])
        Process.system(cmd)

    def _session_handle_actions(self, dc, session, actions):
        extra = session['extra']
        remaining_actions = []
        update_session = False
        print(Color.bold(f" > Session {session['id']}"))
        launch_otf = False

        for action in actions:
            raw = extra.get('raw', {})
            otf = extra.get('otf', {})
            if not raw or 'path' not in raw:
                continue

            try:
                print(f"   - Checking action: {action}")
                if action.startswith('update_raw'):

                    last_movie = raw.get('last_movie', '')
                    raw_path = raw['path']
                    kwargs = {}
                    otf_path = otf.get('path', '')
                    otf_exists = os.path.exists(otf_path)

                    if otf_exists:
                        epuPath = os.path.join(otf['path'], 'EPU')
                        kwargs = {
                            'outputStar': os.path.join(epuPath, 'movies.star'),
                            'backupFolder': epuPath
                        }
                    kwargs['lastMovie'] = last_movie

                    # Get the new raw info
                    raw = EPU.parse_session(raw_path, **kwargs)
                    if last_movie != raw.get('last_movie', ''):
                        extra['raw'] = raw
                        extra['raw']['path'] = raw_path
                        update_session = True
                    else:
                        last_creation = raw.get('last_movie_creation', '')
                        if last_creation:
                            last_movie = raw['last_movie']
                            last_modified = Pretty.parse_datetime(last_creation)
                            print(f"RAW: last: {last_movie}, {last_modified}")
                            now = datetime.now()
                            if (now - last_modified).days >= 1 and not self.active_otf(session):
                                session['status'] = 'finished'
                                otf['status'] = 'finished'
                                update_session = True

                        if not update_session:
                            print(f"      No changes for this session, not updating.")

                    if 'from_server' not in action:
                        update_session = True

                    # Check now for otf status
                    if otf_exists:
                        status = otf.get('status', None)
                        if status == 'created':  # launch otf now
                            n = raw['movies']
                            launch_otf = n > 16
                            msg = 'Launching OTF!' if launch_otf else 'Waiting for more movies'
                            print(f"OTF: folder already CREATED, "
                                  f"input movies {raw['movies']}."
                                  f"\n\t{msg}")
                            if launch_otf:
                                otf['status'] = 'launched'
                                update_session = True

                elif action.startswith('create_otf'):
                    update_session = True
                    self.create_session_otf(session)

                elif action.startswith('launch_otf'):
                    otf['status'] = 'launched'
                    update_session = True
                    launch_otf = True

            except Exception as e:
                print(e)
                traceback.print_exc()
        if update_session:
            print(f"        Updating session {session['name']}")
            extra['actions'] = remaining_actions
            extra['updated'] = Pretty.now()
            dc.update_session(session)

        if launch_otf:
            self.launch_sessions_otf(session)

    def _poll_sessions(self):
        """ Check for actions needed for EMhub's sessions. """
        while True:
            try:
                with open_client() as dc:
                    print(">>> Polling active sessions...")
                    r = dc.request('poll_active_sessions', jsonData={})

                    for s in r.json():
                        print(f"   Handling session: {s['id']}")
                        extra = s['extra']
                        actions = extra.pop('actions', [])
                        self._session_handle_actions(dc, s, actions)
            except Exception as e:
                print("Some error happened: ", str(e))
                print("Waiting 60 seconds before retrying...")
                time.sleep(60)

    def _sessions_update_info(self):
        """ Inspect the files under active sessions and update 'raw' info.
        This job should not be run that frequent since it is not crucial info.
        """
        print(f"{Color.green(Pretty.now())} Updating sessions info...")
        with open_client() as dc:
            for session in dc.get_active_sessions():
                self._session_handle_actions(dc, session,
                                             ['update_raw:from_server'])

    def _sessions_sync_files(self):
        print(f"{Color.warn(Pretty.now())} Synchronizing files...")

    def serve_forever(self, *args, **kwargs):
        print(f"{Color.green(Pretty.now())} Running server\n\taddress: {self._address}")
        # Start a thread with the server -- that thread will then start one
        # more thread for each request
        self._scheduler = BackgroundScheduler()
        # Schedule _poll_sessions function to run now
        self._scheduler.add_job(self._poll_sessions, 'date')
        self._scheduler.add_job(self._sessions_update_info, 'interval', minutes=1)
        # self._scheduler.add_job(self._sessions_sync_files, 'interval', seconds=10)
        self._scheduler.start()

        JsonTCPServer.serve_forever(self, *args, **kwargs)


if __name__ == '__main__':
    with SessionsServer() as server:
        server.serve_forever()
