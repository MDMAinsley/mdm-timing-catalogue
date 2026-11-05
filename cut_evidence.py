"""Versioned complete evidence groups; no partial or runtime-only identity match."""
import re

AUDIO='chromaprint2-fixed-32-v1'
RANGES='file-ranges-sha256-32k-v1'
POSITIONS={AUDIO:(0,16),RANGES:(0,25,75,100)}
HEX=re.compile(r'[0-9a-f]{64}')
MAX_GROUPS=12


def normalize(value):
    if not isinstance(value,(list,tuple)) or not value or len(value)>4:
        raise ValueError('invalid_evidence_group')
    result=[]
    for row in value:
        if (not isinstance(row,(list,tuple)) or len(row)!=3 or not isinstance(row[0],str)
                or row[0] not in POSITIONS or type(row[1]) is not int
                or not isinstance(row[2],str) or not HEX.fullmatch(row[2])):
            raise ValueError('invalid_evidence_anchor')
        result.append(tuple(row))
    methods={row[0] for row in result}
    if len(methods)!=1:
        raise ValueError('mixed_evidence_methods')
    method=next(iter(methods))
    if len(result)!=len(POSITIONS[method]) or {row[1] for row in result}!=set(POSITIONS[method]):
        raise ValueError('incomplete_evidence_group')
    return tuple(sorted(result))


def groups(value):
    if not isinstance(value,(list,tuple)) or not 1<=len(value)<=MAX_GROUPS:
        raise ValueError('invalid_evidence_groups')
    result=tuple(normalize(group) for group in value)
    if len(set(result))!=len(result):
        raise ValueError('duplicate_evidence_group')
    return result
