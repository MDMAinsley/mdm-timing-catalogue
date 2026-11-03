"""Ephemeral in-memory receiver for the installed diagnostic script. No media persistence."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import uuid
from urllib.parse import urlsplit

from detector import Invalid


def validate(value):
    if not isinstance(value, dict) or value.get('schema') != 1:
        raise Invalid('invalid_kodi_bridge_reply')
    if value.get('status'):
        raise Invalid('selected_flix_file_unavailable')
    if set(value) != {'schema', 'url', 'playback_token', 'tmdb_id', 'season', 'episode'}:
        raise Invalid('invalid_kodi_bridge_reply')
    if any(type(value.get(k)) is not int for k in ('tmdb_id', 'season', 'episode')):
        raise Invalid('invalid_kodi_bridge_identity')
    from references import check_identity
    check_identity(value['tmdb_id'], value['season'])
    if not 1 <= value['episode'] <= 10000:
        raise Invalid('invalid_kodi_bridge_identity')
    import re
    if not isinstance(value['playback_token'], str) or not re.fullmatch('[0-9a-f]{32}', value['playback_token']):
        raise Invalid('invalid_kodi_bridge_identity')
    if not isinstance(value['url'], str) or len(value['url']) > 8192:
        raise Invalid('invalid_kodi_bridge_address')
    address = urlsplit(value['url'])
    if address.scheme != 'https' or not address.hostname or address.username or address.password or '|' in value['url']:
        raise Invalid('invalid_kodi_bridge_address')
    return value


def acquire(call):
    # call is the normal RPC transport. Only this known script may be launched.
    details = call('Addons.GetAddonDetails', {'addonid': 'script.mdm.audio.diagnostic', 'properties': ['enabled', 'version']})
    if not details.get('addon', {}).get('enabled'):
        raise Invalid('install_audio_diagnostic_bridge')
    token, event, box = uuid.uuid4().hex, threading.Event(), {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            if self.path != '/'+token:
                self.send_error(404)
                return
            try:
                count = int(self.headers.get('Content-Length', '0'))
                if not 0 < count <= 16384:
                    raise ValueError()
                self.connection.settimeout(2)
                box['value'] = json.loads(self.rfile.read(count))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'OK')
                event.set()
            except Exception:
                self.close_connection = True

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        call('Addons.ExecuteAddon', {'addonid': 'script.mdm.audio.diagnostic',
                                    'params': [str(server.server_port), token], 'wait': False})
        if not event.wait(8):
            raise Invalid('kodi_bridge_reply_timeout')
        return validate(box.get('value'))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
