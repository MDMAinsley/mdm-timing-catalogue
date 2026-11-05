import io
from http.client import IncompleteRead
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import threading

from cut_evidence import normalize
import range_identity as identity

DATA=bytes(range(251))*5000


class Response:
    def __init__(self,data,headers,status=206):
        self.stream=io.BytesIO(data)
        self.headers=headers
        self.status=status
        self.reads=0
    def __enter__(self):
        return self
    def __exit__(self,*args):
        self.stream.close()
    def read1(self,n):
        self.reads+=1
        return self.stream.read(n)


class Host:
    def __init__(self,tag='"stable"'):
        self.tag=tag
        self.requests=[]
        self.responses=[]
        self.modify=lambda response,index:response
    def open(self,request,timeout):
        self.requests.append(request)
        start,end=map(int,request.get_header('Range')[6:].split('-'))
        headers={'Content-Range':f'bytes {start}-{end}/{len(DATA)}','Content-Length':str(identity.BLOCK)}
        if self.tag is not None:
            headers['ETag']=self.tag
        response=self.modify(Response(DATA[start:end+1],headers),len(self.requests))
        self.responses.append(response)
        return response


class RangeIdentityTests(unittest.TestCase):
    def test_four_complete_hashes_cross_host_and_local_file_agree(self):
        a,metrics=identity.fingerprint_http('https://first.invalid/private?token=secret',opener=Host())
        b,_=identity.fingerprint_http('https://second.invalid/another',opener=Host('"another-host-tag"'))
        self.assertEqual(a,b)
        self.assertEqual(metrics['bytes'],4*32768)
        self.assertEqual(metrics['requests'],4)
        self.assertNotIn('secret',str(metrics))
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'episode.mkv'
            p.write_bytes(DATA)
            local,counts=identity.fingerprint_file(p)
            self.assertEqual(a,local)
            self.assertEqual(counts['bytes'],4*32768)
        self.assertEqual(len(normalize(a)),4)

    def test_no_strong_etag_rechecks_prefix_and_tail_under_192k(self):
        for tag in (None,'W/"weak"'):
            host=Host(tag)
            _,metrics=identity.fingerprint_http('https://host.invalid/file',opener=host)
            self.assertEqual(metrics['bytes'],192*1024)
            self.assertEqual(metrics['requests'],6)
            self.assertTrue(all(not r.get_header('If-range') for r in host.requests))

    def test_strong_etag_sends_if_range_and_changed_validator_is_rejected(self):
        host=Host()
        identity.fingerprint_http('https://host.invalid/file',opener=host)
        self.assertTrue(all(r.get_header('If-range')=='"stable"' for r in host.requests[1:]))
        host=Host()
        def change(response,index):
            if index==2:response.headers['ETag']='"changed"'
            return response
        host.modify=change
        with self.assertRaisesRegex(identity.IdentityError,'validator'):
            identity.fingerprint_http('https://host.invalid/file',opener=host)

    def test_changed_body_on_consistency_read_is_rejected(self):
        host=Host(None)
        def change(response,index):
            if index==5:response.stream=io.BytesIO(b'X'*identity.BLOCK)
            return response
        host.modify=change
        with self.assertRaisesRegex(identity.IdentityError,'file_changed'):
            identity.fingerprint_http('https://host.invalid/file',opener=host)

    def test_bad_ranges_and_ignored_ranges_do_not_read_body(self):
        for field,value in (('status',200),('Content-Range','bytes 1-32768/999999'),
                            ('Content-Length','9999999'),('Content-Encoding','gzip')):
            host=Host()
            def change(response,index):
                if field=='status':response.status=value
                else:response.headers[field]=value
                return response
            host.modify=change
            with self.assertRaises(identity.IdentityError):
                identity.fingerprint_http('https://host.invalid/file',opener=host)
            self.assertEqual(host.responses[0].reads,0)

    def test_truncation_changed_length_timeout_and_cancellation(self):
        host=Host()
        host.modify=lambda response,index:Response(b'X',response.headers)
        with self.assertRaisesRegex(identity.IdentityError,'truncated'):
            identity.fingerprint_http('https://host.invalid/file',opener=host)
        host=Host()
        def change(response,index):
            if index==2:response.headers['Content-Range']=response.headers['Content-Range'].split('/')[0]+'/'+str(len(DATA)+1)
            return response
        host.modify=change
        with self.assertRaisesRegex(identity.IdentityError,'file_changed'):
            identity.fingerprint_http('https://host.invalid/file',opener=host)
        reader=identity.HTTPReader('https://host.invalid/file',opener=Host())
        reader.deadline=0
        with self.assertRaisesRegex(identity.IdentityError,'time_limit'):
            reader.read(0)
        with self.assertRaisesRegex(identity.IdentityError,'buffering'):
            identity.fingerprint_http('https://host.invalid/file',cancel=lambda:True,opener=Host())

    def test_file_size_and_each_slot_are_inside_domain_separation(self):
        data=DATA[:identity.BLOCK]
        first=identity.anchor(len(DATA),0,data)
        self.assertNotEqual(first,identity.anchor(len(DATA)+1,0,data))
        self.assertNotEqual(first[2],identity.anchor(len(DATA),25,data)[2])
        with self.assertRaises(ValueError):normalize([first])
        with self.assertRaises(ValueError):normalize([first]*4)
        for value in ('http://host.invalid/file','file:///etc/passwd','https://user:pass@host.invalid/a','https://[bad', 'https://host.invalid/a|Cookie=private'):
            with self.assertRaises(identity.IdentityError):
                identity.fingerprint_http(value,opener=Host())

    def test_real_loopback_ranges_obey_same_four_request_payload_limit(self):
        counts=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                a,b=map(int,self.headers['Range'][6:].split('-'))
                counts.append(b-a+1)
                self.send_response(206)
                self.send_header('Content-Range',f'bytes {a}-{b}/{len(DATA)}')
                self.send_header('Content-Length',str(b-a+1))
                self.send_header('ETag','"fixture"')
                self.end_headers()
                self.wfile.write(DATA[a:b+1])
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            result,metrics=identity.fingerprint_http(f'http://127.0.0.1:{server.server_port}/file',fixture=True)
            self.assertEqual(result,identity.fingerprint_http('https://fixture.invalid/file',opener=Host())[0])
            self.assertEqual(sum(counts),128*1024)
            self.assertEqual(metrics['requests'],4)
        finally:
            server.shutdown();server.server_close();thread.join(2)

    def test_sparse_uninformative_file_is_not_accepted_as_known_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'incomplete.mkv'
            path.write_bytes(b'\0'*(1024*1024))
            with self.assertRaisesRegex(identity.IdentityError,'low_information'):
                identity.fingerprint_file(path)

    def test_broken_chunked_response_becomes_a_safe_retryable_identity_error(self):
        host=Host()
        def broken(response,index):
            def read(n):raise IncompleteRead(b'private media',100)
            response.read1=read
            return response
        host.modify=broken
        with self.assertRaisesRegex(identity.IdentityError,'range_payload_unavailable') as caught:
            identity.fingerprint_http('https://host.invalid/private',opener=host)
        self.assertNotIn('private',str(caught.exception))
