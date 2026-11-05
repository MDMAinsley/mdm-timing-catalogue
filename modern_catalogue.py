"""Strict schema 3: complete independent identity groups, one canonical cut."""
import json
import math
from pathlib import Path

from cut_evidence import HEX,groups,normalize

TIERS=('software generated','community reviewed','trusted reviewer')
LIMIT=2_000_000


def validate(data):
    if not isinstance(data,dict) or set(data)!={'schema','cuts'} or type(data['schema']) is not int or data['schema']!=3:
        raise ValueError('invalid_modern_schema')
    cuts=data['cuts']
    if not isinstance(cuts,dict) or len(cuts)>4000:
        raise ValueError('invalid_modern_cuts')
    for key,row in cuts.items():
        if not isinstance(key,str) or not HEX.fullmatch(key) or not isinstance(row,dict) or set(row)!={'episode','duration','identities','markers'}:
            raise ValueError('invalid_modern_cut')
        ep=row['episode']
        if (not isinstance(ep,(list,tuple)) or len(ep)!=3 or any(type(v) is not int for v in ep)
                or ep[0]<1 or ep[1]<0 or ep[2]<1 or type(row['duration']) not in (int,float)
                or not math.isfinite(row['duration']) or not 60<=row['duration']<=86400):
            raise ValueError('invalid_modern_identity')
        groups(row['identities'])
        if not isinstance(row['markers'],dict) or set(row['markers'])-{'intro','outro'}:
            raise ValueError('invalid_modern_markers')
        for marker in row['markers'].values():
            if (not isinstance(marker,dict) or set(marker)!={'id','revision','start','end','tier'}
                    or not isinstance(marker['id'],str) or not HEX.fullmatch(marker['id'])
                    or type(marker['revision']) is not int or marker['revision']<1 or marker['tier'] not in TIERS
                    or any(type(marker[k]) not in (int,float) or not math.isfinite(marker[k]) for k in ('start','end'))
                    or not 0<=marker['start']<marker['end']<=row['duration']):
                raise ValueError('invalid_modern_marker')
    return data


def read(path):
    with Path(path).open('rb') as stream:
        raw=stream.read(LIMIT+1)
    if len(raw)>LIMIT:
        raise ValueError('modern_catalogue_limit')
    return validate(json.loads(raw))


def matching(data,episode,duration,evidence):
    observed=normalize(evidence)
    return [key for key,cut in data['cuts'].items() if tuple(cut['episode'])==tuple(episode)
            and abs(cut['duration']-duration)<=3 and observed in groups(cut['identities'])]
