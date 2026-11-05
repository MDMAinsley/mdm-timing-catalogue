"""Small selected-file samples identify known copies without media decoding.

This is not a full-file hash or intro detection. It requires complete evidence,
and never exports media bytes, the address, ETag, local paths or file length.
"""
import hashlib
from http.client import HTTPException
import os
import re
import stat
import time
import urllib.error
import urllib.request
from urllib.parse import urljoin,urlsplit

METHOD='file-ranges-sha256-32k-v1'
SLOTS=(0,25,75,100)
BLOCK=32768
MAX_BYTES=6*BLOCK
MIN_SIZE=8*BLOCK
MAX_SIZE=2**63-1


class IdentityError(Exception):
    pass


def offsets(size):
    if type(size) is not int or not MIN_SIZE<=size<=MAX_SIZE:
        raise IdentityError('unsupported_file_size')
    return dict(zip(SLOTS,(0,(size-BLOCK)//4,3*(size-BLOCK)//4,size-BLOCK)))


def anchor(size,slot,data):
    if slot not in SLOTS or len(data)!=BLOCK:
        raise IdentityError('incomplete_range')
    domain=(METHOD+':'+str(size)+':'+str(slot)+':'+str(BLOCK)+':').encode('ascii')
    return METHOD,slot,hashlib.sha256(domain+data).hexdigest()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        return None


def address(value,fixture=False):
    if not isinstance(value,str) or len(value)>8192 or any(c in value for c in ('\r','\n','|')):
        return False
    try:
        u=urlsplit(value)
    except ValueError:
        return False
    return bool(u.hostname and not u.username and not u.password and
                (u.scheme=='https' or fixture and u.scheme=='http' and u.hostname in ('localhost','127.0.0.1')))


class HTTPReader:
    def __init__(self,url,*,seconds=8,cancel=lambda:False,fixture=False,opener=None):
        if not address(url,fixture) or type(seconds) not in (int,float) or not 0<seconds<=12:
            raise IdentityError('unsupported_address_or_budget')
        self.url=url
        self.fixture=fixture
        self.cancel=cancel
        self.deadline=time.monotonic()+seconds
        self.bytes=self.requests=0
        self.size=None
        self.validator=None
        self.opener=opener or urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())

    def check(self):
        if self.cancel():
            raise IdentityError('playback_changed_or_buffering')
        if time.monotonic()>=self.deadline:
            raise IdentityError('identity_time_limit')

    def read(self,start):
        if type(start) is not int or start<0:
            raise IdentityError('invalid_range')
        for redirect in range(3):
            self.check()
            if self.requests>=8 or self.bytes+BLOCK>MAX_BYTES:
                raise IdentityError('identity_read_limit')
            headers={'Range':'bytes={}-{}'.format(start,start+BLOCK-1),'Accept-Encoding':'identity','User-Agent':'MDM-Skip-Cut/1'}
            if self.validator:
                headers['If-Range']=self.validator
            request=urllib.request.Request(self.url,headers=headers)
            self.requests+=1
            try:
                response=self.opener.open(request,timeout=max(.05,min(3,self.deadline-time.monotonic())))
            except urllib.error.HTTPError as error:
                location=error.headers.get('Location')
                code=error.code
                error.close()
                if code not in (301,302,303,307,308) or redirect==2:
                    raise IdentityError('range_request_unavailable') from None
                target=urljoin(self.url,location) if location else ''
                if not address(target,self.fixture) or urlsplit(self.url).scheme=='https' and urlsplit(target).scheme!='https':
                    raise IdentityError('unsafe_redirect')
                self.url=target
                continue
            except (OSError,ValueError,HTTPException):
                raise IdentityError('range_request_unavailable') from None
            with response:
                self.check()
                match=re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',response.headers.get('Content-Range',''))
                if response.status!=206 or not match or response.headers.get('Content-Encoding','identity').lower()!='identity':
                    raise IdentityError('strict_ranges_required')
                first,last,size=map(int,match.groups())
                if first!=start or last!=start+BLOCK-1 or size<=last or size>MAX_SIZE:
                    raise IdentityError('invalid_content_range')
                declared=response.headers.get('Content-Length')
                if declared is not None and declared!=str(BLOCK):
                    raise IdentityError('invalid_range_length')
                if self.size is not None and self.size!=size:
                    raise IdentityError('file_changed')
                tag=response.headers.get('ETag')
                # Only strong quoted entity tags can be used in If-Range.
                strong=tag if isinstance(tag,str) and re.fullmatch(r'"[^"\r\n]{1,200}"',tag) else None
                if self.size is not None and strong!=self.validator:
                    raise IdentityError('file_validator_changed')
                self.size=size
                self.validator=strong
                chunks=[]
                remaining=BLOCK
                while remaining:
                    self.check()
                    try:
                        chunk=response.read1(min(remaining,8192))
                    except (OSError,HTTPException):
                        raise IdentityError('range_payload_unavailable') from None
                    if not chunk:
                        raise IdentityError('truncated_range')
                    self.bytes+=len(chunk)
                    remaining-=len(chunk)
                    chunks.append(chunk)
                self.check()
                return b''.join(chunks)
        raise IdentityError('redirect_limit')


def fingerprint_http(url,*,seconds=8,cancel=lambda:False,fixture=False,opener=None):
    began=time.monotonic()
    reader=HTTPReader(url,seconds=seconds,cancel=cancel,fixture=fixture,opener=opener)
    first=reader.read(0)
    starts=offsets(reader.size)
    anchors=[anchor(reader.size,0,first)]
    informative=int(len(set(first))>=16)
    last=None
    for slot in SLOTS[1:]:
        last=reader.read(starts[slot])
        informative+=int(len(set(last))>=16)
        anchors.append(anchor(reader.size,slot,last))
    if informative<2:
        raise IdentityError('low_information_samples')
    if not reader.validator:
        # Non-ETag servers need bounded consistency reads. Never trust weak tags.
        if reader.read(0)!=first or reader.read(starts[100])!=last:
            raise IdentityError('file_changed')
    reader.check()
    return anchors,dict(method=METHOD,bytes=reader.bytes,requests=reader.requests,
                        seconds=round(time.monotonic()-began,4),consistency='strong_etag' if reader.validator else 'rechecked_ranges')


def fingerprint_file(path,*,seconds=8,cancel=lambda:False):
    began=time.monotonic()
    if not isinstance(path,(str,os.PathLike)) or not os.path.isabs(path) or str(path).startswith(('\\\\','//')):
        raise IdentityError('local_regular_file_required')
    with open(path,'rb') as stream:
        before=os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise IdentityError('local_regular_file_required')
        starts=offsets(before.st_size)
        anchors=[]
        informative=0
        for slot in SLOTS:
            if cancel():
                raise IdentityError('playback_changed_or_buffering')
            if time.monotonic()-began>=seconds:
                raise IdentityError('identity_time_limit')
            stream.seek(starts[slot])
            data=stream.read(BLOCK)
            informative+=int(len(set(data))>=16)
            anchors.append(anchor(before.st_size,slot,data))
        if informative<2:
            raise IdentityError('low_information_samples')
        after=os.fstat(stream.fileno())
        if (before.st_size,before.st_mtime_ns,before.st_ino)!=(after.st_size,after.st_mtime_ns,after.st_ino):
            raise IdentityError('file_changed')
    return anchors,dict(method=METHOD,bytes=4*BLOCK,requests=0,seconds=round(time.monotonic()-began,4),consistency='local_stat')
