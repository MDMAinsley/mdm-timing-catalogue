"""Finite selected-file range relay; native decoder never sees the signed upstream URL."""
from collections import OrderedDict
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import re
import threading
import time
import urllib.request
import urllib.error
from urllib.parse import urlsplit
import uuid

from detector import Invalid


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Reader:
    BLOCK = 262144

    def __init__(self, url, cancelled=lambda: False, max_bytes=64*1024*1024,
                 max_seconds=60, max_requests=256, fixture=False, block_size=262144):
        if type(block_size) is not int or block_size not in (65536, 262144, 1048576):
            raise Invalid('invalid_stream_block_size')
        self.BLOCK = block_size
        u = urlsplit(url)
        if (u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password or
                '|' in url or '\r' in url or '\n' in url or
                (u.scheme != 'https' and not (fixture and u.hostname in ('127.0.0.1', 'localhost')))):
            raise Invalid('unsupported_stream_address')
        self.url, self.cancelled = url, cancelled
        self.deadline = time.monotonic()+max_seconds
        self.max_bytes, self.max_requests = max_bytes, max_requests
        self.bytes = self.requests = 0
        self.size = self.etag = None
        self.blocks = OrderedDict()
        self.error = None
        self.error_type = None
        self.lock = threading.RLock()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def check(self):
        if self.error:
            raise Invalid(self.error)
        if self.cancelled():
            self.error = 'playback_changed_or_buffering'
        elif time.monotonic() >= self.deadline:
            self.error = 'stream_analysis_timeout'
        if self.error:
            raise Invalid(self.error)

    def block(self, offset):
        with self.lock:
            self.check()
            base = offset//self.BLOCK*self.BLOCK
            if base in self.blocks:
                self.blocks.move_to_end(base)
                return base, self.blocks[base]
            if self.size is not None and base >= self.size:
                return base, b''
            count = self.BLOCK if self.size is None else min(self.BLOCK, self.size-base)
            if self.requests >= self.max_requests or self.bytes+count > self.max_bytes:
                self.error = 'stream_byte_or_request_limit'
                raise Invalid(self.error)
            self.requests += 1
            request = urllib.request.Request(self.url, headers={
                'Range': f'bytes={base}-{base+count-1}', 'Accept-Encoding': 'identity',
                'User-Agent': 'MDM-Audio-PC-Experiment/1'})
            try:
                response = self.opener.open(request, timeout=max(.05, min(8, self.deadline-time.monotonic())))
                with response:
                    header = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('Content-Range', ''))
                    if response.status != 206 or not header or response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
                        raise Invalid('stream_range_unavailable')
                    start, end, size = map(int, header.groups())
                    if start != base or end != min(base+count-1, size-1) or not 0 <= start <= end < size:
                        raise Invalid('invalid_stream_range')
                    etag = response.headers.get('ETag')
                    if self.size is not None and (size != self.size or etag != self.etag):
                        raise Invalid('stream_file_changed')
                    self.size, self.etag = size, etag
                    chunks, left = [], end-start+1
                    while left:
                        self.check()
                        piece = response.read1(min(left, 16384))
                        if not piece:
                            raise Invalid('truncated_stream_range')
                        self.bytes += len(piece)
                        chunks.append(piece)
                        left -= len(piece)
                    data = b''.join(chunks)
            except Invalid as exc:
                self.error = str(exc)
                raise
            except urllib.error.HTTPError as exc:
                self.error = 'stream_http_'+str(exc.code)
                raise Invalid(self.error) from None
            except (TimeoutError,):
                self.error = 'stream_read_timeout'
                raise Invalid(self.error) from None
            except urllib.error.URLError as exc:
                self.error = 'stream_read_timeout' if isinstance(exc.reason, TimeoutError) else 'stream_connection_failed'
                raise Invalid(self.error) from None
            except Exception as exc:
                self.error_type = type(exc).__name__
                self.error = 'stream_access_failed'
                raise Invalid(self.error) from None
            self.blocks[base] = data
            while len(self.blocks) > 8:
                self.blocks.popitem(last=False)
            return base, data


@contextmanager
def relay(reader):
    token = '/'+uuid.uuid4().hex

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path != token:
                self.send_error(404)
                return
            try:
                reader.check()
                if reader.size is None:
                    reader.block(0)
                raw = self.headers.get('Range')
                matched = re.fullmatch(r'bytes=(\d+)-(\d*)', raw) if raw else None
                if raw and not matched:
                    self.send_error(416)
                    return
                start = int(matched[1]) if matched else 0
                end = min(int(matched[2]), reader.size-1) if matched and matched[2] else reader.size-1
                if not 0 <= start <= end < reader.size:
                    self.send_error(416)
                    return
                self.send_response(206 if matched else 200)
                self.send_header('Content-Length', str(end-start+1))
                self.send_header('Accept-Ranges', 'bytes')
                self.send_header('Content-Type', 'application/octet-stream')
                if matched:
                    self.send_header('Content-Range', f'bytes {start}-{end}/{reader.size}')
                self.end_headers()
                offset = start
                while offset <= end:
                    reader.check()
                    base, block = reader.block(offset)
                    part = block[offset-base:offset-base+min(32768, end-offset+1)]
                    if not part:
                        break
                    self.wfile.write(part)
                    offset += len(part)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                self.close_connection = True

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}{token}'
    finally:
        reader.error = reader.error or 'capture_finished'
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
