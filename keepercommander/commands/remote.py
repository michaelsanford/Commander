#  _  __
# | |/ /___ ___ _ __  ___ _ _ ®
# | ' </ -_) -_) '_ \/ -_) '_|
# |_|\_\___\___| .__/\___|_|
#              |_|
#
# Keeper Commander
# Copyright 2022 Keeper Security Inc.
# Contact: ops@keepersecurity.com
#
import argparse
import hashlib
import json
import logging
import pprint
from time import time
from urllib.parse import urlparse

from .base import Command, raise_parse_exception, suppress_exit
from .recordv3 import RecordAddCommand
from keepercommander import api
from keepercommander.subfolder import try_resolve_path

import requests
import websocket
from jwcrypto.jwt import JWK, JWKSet, JWT


REMOTE_CONTROL_AUTH_ID = '9qn0mitn5d'
REMOTE_CONTROL_AUTH_API = f'https://{REMOTE_CONTROL_AUTH_ID}.execute-api.us-east-1.amazonaws.com/'
AUTH_URL = 'https://xmr2imqr1d.execute-api.us-east-1.amazonaws.com/'
JWK_FIELDS = {
    'alg': 'text:alg',
    'api': 'url:api',
    'aud': 'text:aud',
    'iss': 'url:iss',
    'kid': 'text:kid',
    'key': 'keyPair:key'
}
JWT_EXP_DELTA = 7200  # 2 hours


def register_commands(commands):
    commands['remote'] = RemoteCommand()


def register_command_info(aliases, command_info):
    command_info[remote_parser.prog] = remote_parser.description


remote_subcommands = [
    'user add', 'minion add'
]
remote_parser = argparse.ArgumentParser(prog='remote', description='Run commands on remote minions')
remote_parser.add_argument('--force', dest='force', action='store_true', help='Force record creation when exists')
remote_parser.add_argument(
    '-f', '--folder', dest='remote_folder', action='store', help='Remote control record folder'
)
remote_parser.add_argument(
    '-k', '--root-key', dest='root_key', action='store', help='Root key record for signing stored as JWK record type'
)
remote_parser.add_argument(
    '-r', '--role', dest='role', action='append', help='User role that can be used multiple times'
)
remote_parser.add_argument(
    '-u', '--user-id', dest='user_id', action='store', help='User id'
)
remote_parser.add_argument(
    '-m', '--minion-id', dest='minion_id', action='store', help='Minion id'
)
remote_parser.add_argument(
    '-e', '--expire-token-delta', type=int, dest='exp_delta', action='store', default=JWT_EXP_DELTA,
    help='Expiration delta of minion authentication token in seconds'
)
remote_parser.add_argument(
    'command', type=str, action='store', nargs="*", help='One of: "{}"'.format('", "'.join(remote_subcommands))
)
remote_parser.error = raise_parse_exception
remote_parser.exit = suppress_exit


def find_folder_record(params, base_folder, record_name, v3_enabled):
    folder_uid = base_folder.uid
    if folder_uid in params.subfolder_record_cache:
        for uid in params.subfolder_record_cache[folder_uid]:
            rv = params.record_cache[uid].get('version') if params.record_cache and uid in params.record_cache else None
            if rv == 4 or rv == 5:
                continue  # skip fileRef and application records - they use file-report command
            if not v3_enabled and rv in (3, 4):
                continue  # skip record types when not enabled
            r = api.get_record(params, uid)
            if r.title.lower() == record_name.lower():
                return r

    return None


def get_folder(params, folder_path):
    folder = params.folder_cache.get(params.current_folder, params.root_folder)
    rs = try_resolve_path(params, folder_path)
    if rs is not None:
        folder, name = rs
        if len(name) > 0:
            return None
    return folder


def get_record(params, record_path):
    folder = None
    name = None
    if record_path:
        rs = try_resolve_path(params, record_path)
        if rs is not None:
            folder, name = rs

    if folder is None or name is None:
        return None

    if name in params.record_cache:
        return api.get_record(params, name)
    else:
        return find_folder_record(params, folder, name, v3_enabled=True)


def get_user_public_jwk(params, options_dict=None):
    if not options_dict:
        options_dict = {'use': 'sig'}
    options_dict['alg'] = 'RS256'
    user_public_jwk = JWK.from_pem(params.rsa_key.public_key().exportKey(format='PEM'))
    user_public_jwk.update(**options_dict)
    return user_public_jwk


def get_auth_token(record, login, scopes, ent_id=None, public_jwk=None, exp_delta=JWT_EXP_DELTA):
    token_vars = {
        'login': login,
        'scope': ' '.join(scopes)
    }
    for f in record.custom_fields:
        for k, v in JWK_FIELDS.items():
            if f['name'] == v:
                token_vars[k] = f['value']
    for k, v in JWK_FIELDS.items():
        if v.startswith('url:') and k not in token_vars:
            token_vars[k] = record.login_url
            break

    token_vars['exp'] = int(time()) + exp_delta

    payload = {k: token_vars[k] for k in ['exp', 'aud', 'iss', 'kid', 'login', 'scope']}
    if ent_id:
        payload['ent-id'] = hashlib.sha256(ent_id.to_bytes(4, 'big')).hexdigest()
    if public_jwk:
        # This is the format needed for the issuer jwks.json. It isn't helpful here, though,
        # because format should be applied to export of entire JWKSet
        # payload['public-jwk'] = json.dumps(public_jwk.export_public(as_dict=True), sort_keys=True, indent=2)
        payload['public-jwk'] = public_jwk.export_public()

    headers = {k: token_vars[k] for k in ['alg', 'kid']}
    jwt_token = JWT(header=headers, claims=payload)
    key = JWK.from_pem(token_vars['key']['privateKey'].encode())
    jwt_token.make_signed_token(key)

    logging.debug(f'JWT token payload: {pprint.pformat(payload)}')
    logging.debug(f'JWT token headers: {pprint.pformat(headers)}')
    return token_vars['api'], jwt_token.serialize(), headers, payload


def decode_token(raw_jwt_token):
    jwt_token = JWT(jwt=raw_jwt_token).token
    payload = json.loads(jwt_token.objects['payload'])
    headers = jwt_token.jose_header
    return payload, headers


def get_ws_response(ws, print_msg=True):
    response = ''
    while response is not None:
        try:
            resp_json = ws.recv()
        except ConnectionAbortedError:
            logging.warning('Websocket connection timed out')
            response = None
        except websocket.WebSocketTimeoutException:
            # This is actually not an error; there are just no more responses.
            response = None

        else:
            try:
                resp_dict = json.loads(resp_json)
            except json.JSONDecodeError:
                logging.warning(f'Invalid websocket response: {resp_dict}')
                response = resp_json

            else:
                if isinstance(resp_dict, dict) and len(resp_dict) > 0:
                    message = resp_dict.pop('message', False)
                    rotate_log = message.get('rotate_log', False) if isinstance(message, dict) else False
                    if rotate_log:
                        response = rotate_log
                    else:
                        response = []
                        members = resp_dict.pop('members', False)
                        command = resp_dict.pop('command', False)
                        if members:
                            member_list = '\n    '.join([''] + members)
                            response.append(f'members: {member_list}')
                        if command:
                            command_str = ' '.join(command)
                            response.append(f'command: {command_str}')
                        if message:
                            msg_from = resp_dict.pop('from', 'anonymous')
                            response.append(f'{msg_from}: {message}')
                        if len(resp_dict) > 0:
                            response.append(json.dumps(resp_dict))

                    if print_msg:
                        print('\n'.join(response))
                else:
                    logging.warning(f'Invalid websocket response: {resp_dict}')
                    response = resp_dict


class RemoteCommand(Command):
    def get_parser(self):
        return remote_parser

    def execute(self, params, **kwargs):
        v3_enabled = params.settings.get('record_types_enabled') if params.settings and isinstance(params.settings.get('record_types_enabled'), bool) else False
        if not v3_enabled:
            logging.warning(f"Record types are needed for remote commands")
            return

        force = kwargs.get('force', False)
        remote_command = kwargs.get('command')
        if len(remote_command) == 0:
            logging.warning('Please specify a subcommand to run')
            return

        remote_folder_path = kwargs.get('remote_folder')
        if remote_folder_path:
            remote_folder = get_folder(params, remote_folder_path)
            if remote_folder:
                logging.info(f'Found folder {remote_folder.name}')
            else:
                logging.warning(f"Can't find specified folder {remote_folder_path}")
        else:
            remote_folder = None

        root_key_path = kwargs.get('root_key')
        if root_key_path:
            root_key = get_record(params, root_key_path)
            if root_key:
                logging.info(f'Found root key {root_key.title}')
            else:
                logging.warning(f"Can't find root key {root_key_path}")
        else:
            root_key = None

        user_login = kwargs.get('user_id') or params.user
        ent_id = params.license.get('enterprise_id')
        remote_obj = remote_command[0]
        remote_action = remote_command[1] if len(remote_command) > 1 else None

        if remote_obj == 'enterprise':
            role = 'admin'
            if not root_key:
                logging.warning('--root-key option required')
                return

            # ent_md5 = hashlib.md5(ent_id.to_bytes(4, 'big')).hexdigest()
            api_url, jwt_token, jwt_headers, jwt_claims = get_auth_token(root_key, login=user_login, scopes=[role])
            auth_url = f'{REMOTE_CONTROL_AUTH_API}enterprise'
            if remote_action == 'add':
                auth_url += f'/{ent_id}'
                iss = jwt_claims['iss']
                bucket = urlparse(iss).netloc.split('.')[0]
                new_jwks = JWKSet()
                new_jwks.add(get_user_public_jwk(params))
                data = {'iss': iss, 'bucket': bucket, 'folder': str(ent_id), 'jwks': new_jwks.export(as_dict=True)}
                r = requests.put(auth_url, headers={'Authorization': jwt_token}, json=data)
                pprint.pprint(json.loads(r.text))
            elif remote_action == 'check':
                r = requests.get(auth_url, headers={'Authorization': jwt_token})
                pprint.pprint(json.loads(r.text))
                if r.status_code == 200:
                    logging.info('Enterprise check successful')
                else:
                    logging.warning('Enterprise check failed')

        if remote_obj == 'minion':
            minion_id = kwargs.get('minion_id')
            if not minion_id:
                logging.warning('The --minion-id (-m) option is required')
                return

            if remote_action == 'add':
                if not remote_folder:
                    logging.warning('The --folder (-f) option is required')
                    return
                if not root_key:
                    logging.warning('--root-key option required')
                    return

                role = 'minion'
                jwt_exp_delta = kwargs.get('exp_delta', JWT_EXP_DELTA)
                api_url, jwt_token, jwt_headers, jwt_claims = get_auth_token(
                    root_key, login=minion_id, scopes=[role], ent_id=ent_id, exp_delta=jwt_exp_delta
                )

                command = RecordAddCommand()
                data = json.dumps({'type': 'remote-minion', 'title': minion_id, 'fields': [
                    {'type': 'login', 'value': [minion_id]},
                    {'type': 'url', 'label': 'api', 'value': [api_url]},
                    {'type': 'secret', 'label': 'JWT token', 'value': [jwt_token]},
                    {'type': 'keyPair', 'label': 'public key', 'value': [{'publicKey': ''}]}
                ]})
                command.execute(params, folder=remote_folder.uid, data=data, force=force)
                logging.info('Record for minion has been added')

            elif remote_action in ('cmd', 'exit', 'ping'):
                cmd = remote_command[2:] if remote_action == 'cmd' else remote_command[1:]
                ws_connections = getattr(params, 'ws_connections', {})
                ws = ws_connections.get(user_login)
                if ws:
                    ws.send(json.dumps(
                        {'action': 'send', 'type': 'command', 'to': minion_id, 'message': cmd}
                    ))
                    get_ws_response(ws)
                    if cmd[0] == 'exit':
                        get_ws_response(ws)
                else:
                    logging.warning(f"Can't find connection for {user_login}")

        if remote_obj == 'user':
            role = 'user'
            minion = kwargs.get('minion_id')
            if remote_action in ('disconnect', 'list', 'receive'):
                ws_connections = getattr(params, 'ws_connections', {})
                ws = ws_connections.get(user_login)
                if ws:
                    if remote_action == 'disconnect':
                        ws_connections.pop(user_login).close(timeout=3)
                        logging.info(f'{user_login} disconnected')

                    elif remote_action == 'receive':
                        get_ws_response(ws)

                    elif remote_action == 'list':
                        action_dict = {'action': 'list'}
                        if len(remote_command) > 2:
                            action_dict['role'] = remote_command[2]
                        ws.send(json.dumps(action_dict))
                        get_ws_response(ws)

                    elif remote_action == 'connectSocket':
                        if minion:
                            ws.send(json.dumps({'action': 'connectSocket', 'to': minion}))
                            logging.info(f'Socket connection to {minion} requested')
                        else:
                            logging.warning(f'Minion (--minion-id) not specified')

                else:
                    logging.warning(f"Can't find connection for {user_login}")

            elif remote_action == 'add':
                if not root_key:
                    logging.warning('--root-key option required')
                    return

                ws_url, jwt_token, jwt_headers, jwt_claims = get_auth_token(root_key, login=user_login, scopes=[role])
                user_jwk_dict = get_user_public_jwk(params).export_public(as_dict=True)
                user_kid = user_jwk_dict['kid']
                auth_url = f'{REMOTE_CONTROL_AUTH_API}users/{user_kid}'
                data = {'jwk_key': user_jwk_dict}
                r = requests.put(auth_url, headers={'Authorization': jwt_token}, json=data)
                pprint.pprint(json.loads(r.text))

            elif remote_action in ('check', 'connect'):
                if not root_key:
                    logging.warning('--root-key option required')
                    return

                api_url, jwt_token, jwt_headers, jwt_claims = get_auth_token(
                    root_key, login=user_login, scopes=[role], ent_id=ent_id, public_jwk=get_user_public_jwk(params)
                )

                if remote_action == 'check':
                    auth_url = f'{AUTH_URL}{role}'
                    r = requests.get(auth_url, headers={'Authorization': jwt_token})
                    pprint.pprint(json.loads(r.text))
                    if r.status_code == 200:
                        logging.info('User check successful')
                    else:
                        logging.warning('User check failed')
                else:
                    ws_connections = getattr(params, 'ws_connections', {})
                    ws = ws_connections.get(user_login)
                    if ws is None:
                        headers = {'Authorization': jwt_token, 'AuthRole': role, 'AuthUser': user_login}
                        ws = websocket.WebSocket()
                        ws.connect(api_url, timeout=3, header=headers)
                        ws_connections[user_login] = ws
                        params.ws_connections = ws_connections
                        logging.info(f'{user_login} connected')
                        if minion:
                            ws.send(json.dumps(
                                {'action': 'send', 'type': 'command', 'to': minion, 'message': ['ping']}
                            ))
                            get_ws_response(ws)
                    else:
                        logging.warning(f'User {user_login} is already connected')
