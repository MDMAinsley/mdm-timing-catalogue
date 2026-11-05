"""Server-administrator review tools and privacy-checked public release bundles."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from catalogue import Cut, corrected_range, match_cut
from export_catalogue import compile_snapshot
from review_queue import build_queue
from review_registry import Registry
from validate_snapshot import validate
from cut_evidence import AUDIO
import modern_catalogue


def receipts(directory):
    path=Path(directory)/'inbox.sqlite'
    if not path.exists():
        return []
    with closing(sqlite3.connect(path)) as db:
        db.row_factory=sqlite3.Row
        # An old inbox without ingestion order is deliberately not guessed here.
        return [dict(row) for row in db.execute('''SELECT inbox.*,ingestion.sequence FROM inbox
                JOIN ingestion ON inbox.id=ingestion.receipt ORDER BY ingestion.sequence''')]


def process(directory):
    registry=Registry(directory)
    linked=0
    for row in receipts(directory):
        body=json.loads(row['payload'])
        result=registry.apply(row['actor'],row['id'],row['sequence'],body)
        if result:
            with closing(sqlite3.connect(Path(directory)/'inbox.sqlite')) as db:
                with db:
                    db.execute("UPDATE inbox SET state='feedback_recorded' WHERE id=?",(row['id'],))
            linked+=1
    return linked


def correction_candidates(directory):
    registry=Registry(directory)
    with registry.db() as db:
        cuts=registry._cuts(db)
        evidence=registry._groups(db,cuts)
        actors={r['id']:dict(r) for r in db.execute('''SELECT actors.*,catalogue_policy.eligible
                FROM actors LEFT JOIN catalogue_policy ON actors.id=catalogue_policy.actor''')}
    latest={}
    for row in receipts(directory):
        body=json.loads(row['payload'])
        identity=actors.get(row['actor'])
        if (body.get('assessment')!='correction' or not body.get('operation') or not body.get('anchors')
                or not identity or identity['linked_to'] is not None):
            continue
        if identity['kind']=='anonymous' and not identity['eligible']:
            continue
        matches=Registry.matching(Cut(tuple(body['episode']),body['duration'],tuple(tuple(a) for a in body['anchors'])),cuts,evidence)
        if len(matches)!=1:
            continue
        cut=matches[0]
        latest[(cut,body['marker'],identity['kind'],row['actor'])]=(body['start'],body['end'])
    groups={}
    for (cut,marker,kind,actor),bounds in latest.items():
        groups.setdefault((cut,marker),{}).setdefault(kind,[]).append((actor,*bounds))
    output=[]
    for (cut,marker),cohorts in groups.items():
        signed=cohorts.get('signed_in',[])
        guest=cohorts.get('anonymous',[])
        # A signed-in quorum has priority, including when it has no agreeing cluster.
        selected=signed if len(signed)>=5 else guest
        minimum=5 if len(signed)>=5 else 20
        candidate=corrected_range(selected,cuts[cut].duration,minimum)
        if candidate:
            output.append(dict(cut=cut,episode=cuts[cut].episode,marker=marker,start=candidate[0],end=candidate[1],
                               signed_in=len(signed),eligible_anonymous=len(guest),state='requires_review'))
    return output


def report(directory):
    registry=Registry(directory)
    cuts,proposals,signed,trusted,options=registry.ledger()
    rows=receipts(directory)
    states={}
    pending=[]
    for row in rows:
        states[row['state']]=states.get(row['state'],0)+1
        if row['state']!='feedback_recorded':
            body=json.loads(row['payload'])
            pending.append(dict(receipt=row['id'],episode=body['episode'],duration=body['duration'],
                marker=body['marker'],start=body['start'],end=body['end'],assessment=body.get('assessment'),
                audio_evidence=bool(body['anchors'] and body['anchors'][0][0]==AUDIO),
                evidence_method=body['anchors'][0][0] if body['anchors'] else None,state=row['state']))
    queue=build_queue(cuts,proposals,signed,trusted,**options)
    return dict(schema=1,cuts=len(cuts),received=len(rows),states=states,pending=pending,
                flagged=queue['entries'],corrections=correction_candidates(directory))


def bundle(directory,destination):
    """Create a new immutable directory; the publisher only reads named public files."""
    process(directory)
    registry=Registry(directory)
    cuts,proposals,signed,trusted,options=registry.ledger()
    all_snapshot=compile_snapshot(cuts,proposals,signed,trusted,**options)
    identities=registry.evidence()
    legacy_cuts={}
    for key,row in all_snapshot['cuts'].items():
        audio=next((group for group in identities[key] if group[0][0]==AUDIO),None)
        if audio:
            legacy_cuts[key]=dict(row,anchors=audio)
    snapshot=dict(schema=2,cuts=legacy_cuts)
    modern=dict(schema=3,cuts={key:dict(episode=row['episode'],duration=row['duration'],
                 identities=identities[key],markers=row['markers']) for key,row in all_snapshot['cuts'].items()})
    modern_catalogue.validate(modern)
    queue=build_queue(cuts,proposals,signed,trusted,**options)
    target=Path(destination)
    target.mkdir(parents=True,exist_ok=False)
    # Keep files private until a publisher validates and copies just these exports.
    try:
        os.chmod(target,0o700)
        for name,value in [('playback-snapshot.json',snapshot),('playback-snapshot-v3.json',modern),('review-queue.json',queue)]:
            raw=(json.dumps(value,sort_keys=True,indent=2,allow_nan=False)+'\n').encode()
            if len(raw)>2_000_000:
                raise ValueError('public_export_limit')
            (target/name).write_bytes(raw)
        validate(target/'playback-snapshot.json')
        validate_queue(target/'review-queue.json')
        files={name:hashlib.sha256((target/name).read_bytes()).hexdigest()
               for name in ('playback-snapshot.json','playback-snapshot-v3.json','review-queue.json')}
        manifest=dict(schema=1,files=files,cuts=len(modern['cuts']),
                      markers=sum(len(c['markers']) for c in modern['cuts'].values()),
                      review_entries=len(queue['entries']))
        (target/'catalogue-release.json').write_text(json.dumps(manifest,sort_keys=True,indent=2)+'\n',encoding='utf8')
    except BaseException:
        # Incomplete bundles remain for inspection but can never pass publication.
        (target/'INCOMPLETE').touch()
        raise
    return manifest


def validate_queue(path):
    raw=Path(path).read_bytes()
    if len(raw)>2_000_000:
        raise ValueError('review_export_limit')
    data=json.loads(raw)
    if not isinstance(data,dict) or set(data)!={'schema','entries'} or type(data['schema']) is not int or data['schema']!=1:
        raise ValueError('invalid_queue_schema')
    if not isinstance(data['entries'],dict) or len(data['entries'])>4000:
        raise ValueError('invalid_queue_entries')
    expected={'cut','episode','duration','marker','revision','start','end','good','bad','reason','signed_in','anonymous','eligible_anonymous'}
    from submission_api import HEX
    for key,row in data['entries'].items():
        if not isinstance(key,str) or not HEX.fullmatch(key) or not isinstance(row,dict) or set(row)!=expected:
            raise ValueError('invalid_queue_fields')
        if not isinstance(row['cut'],str) or not HEX.fullmatch(row['cut']):
            raise ValueError('invalid_queue_cut')
        # Reuse the strict timing validator without accepting account/device data.
        from submission_api import validate_proposal
        validate_proposal(dict(episode=row['episode'],duration=row['duration'],anchors=[],marker=row['marker'],
                               start=row['start'],end=row['end'],origin='manual'))
        if type(row['revision']) is not int or row['revision']<1 or row['reason']!='ten_bad_ratings':
            raise ValueError('invalid_queue_revision')
        for group in ('signed_in','anonymous','eligible_anonymous'):
            counts=row[group]
            if not isinstance(counts,dict) or set(counts)!={'good','bad','total','positive_percent'}:
                raise ValueError('invalid_queue_counts')
            if any(type(counts[k]) is not int or counts[k]<0 for k in ('good','bad','total')) or counts['total']!=counts['good']+counts['bad']:
                raise ValueError('invalid_queue_counts')
            expected_percent=round(100*counts['good']/counts['total'],2) if counts['total'] else None
            if counts['positive_percent']!=expected_percent:
                raise ValueError('invalid_queue_percent')
        if (type(row['good']) is not int or type(row['bad']) is not int
                or row['good']!=row['signed_in']['good']+row['eligible_anonymous']['good']
                or row['bad']!=row['signed_in']['bad']+row['eligible_anonymous']['bad'] or row['bad']<10):
            raise ValueError('invalid_queue_totals')
    return data


def validate_bundle(directory):
    directory=Path(directory)
    if (directory/'INCOMPLETE').exists():
        raise ValueError('incomplete_export')
    manifest=json.loads((directory/'catalogue-release.json').read_bytes())
    if not isinstance(manifest,dict) or set(manifest)!={'schema','files','cuts','markers','review_entries'} or type(manifest['schema']) is not int or manifest['schema']!=1:
        raise ValueError('invalid_release_manifest')
    old_files={'playback-snapshot.json','review-queue.json'}
    new_files=old_files|{'playback-snapshot-v3.json'}
    if set(manifest['files']) not in (old_files,new_files):
        raise ValueError('invalid_release_files')
    if any(type(manifest[k]) is not int or manifest[k]<0 for k in ('cuts','markers','review_entries')):
        raise ValueError('invalid_release_counts')
    validate(directory/'playback-snapshot.json')
    legacy=json.loads((directory/'playback-snapshot.json').read_bytes())
    if set(manifest['files'])==new_files:
        modern=modern_catalogue.read(directory/'playback-snapshot-v3.json')
    else:
        # Old published bundles remain verifiable during a rolling upgrade.
        modern=modern_catalogue.validate(dict(schema=3,cuts={key:dict(episode=row['episode'],duration=row['duration'],
            identities=[row['anchors']],markers=row['markers']) for key,row in legacy['cuts'].items()}))
    for key,row in legacy['cuts'].items():
        modern_row=modern['cuts'].get(key)
        if (not modern_row or row['episode']!=modern_row['episode'] or row['duration']!=modern_row['duration']
                or row['markers']!=modern_row['markers'] or row['anchors'] not in modern_row['identities']):
            raise ValueError('legacy_and_modern_catalogue_disagree')
    cuts=len(modern['cuts'])
    markers=sum(len(row['markers']) for row in modern['cuts'].values())
    queue=validate_queue(directory/'review-queue.json')
    if any(row['cut'] not in modern['cuts'] or row['episode']!=modern['cuts'][row['cut']]['episode']
           or row['duration']!=modern['cuts'][row['cut']]['duration'] for row in queue['entries'].values()):
        raise ValueError('review_queue_cut_mismatch')
    if (cuts,markers,len(queue['entries']))!=(manifest['cuts'],manifest['markers'],manifest['review_entries']):
        raise ValueError('release_count_mismatch')
    for name,expected in manifest['files'].items():
        if hashlib.sha256((directory/name).read_bytes()).hexdigest()!=expected:
            raise ValueError('release_hash_mismatch')
    return manifest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data',type=Path,required=True)
    commands=parser.add_subparsers(dest='command',required=True)
    commands.add_parser('report')
    commands.add_parser('process')
    export=commands.add_parser('export')
    export.add_argument('destination',type=Path)
    admit=commands.add_parser('admit')
    admit.add_argument('receipt')
    admit.add_argument('--confirmed-cut',action='store_true')
    admit.add_argument('--software',action='store_true')
    admit.add_argument('--operator',required=True)
    role=commands.add_parser('policy')
    role.add_argument('actor',type=int)
    role.add_argument('--eligible',action='store_true')
    role.add_argument('--trusted',action='store_true')
    role.add_argument('--operator',required=True)
    review=commands.add_parser('review')
    review.add_argument('target')
    review.add_argument('reviewer',type=int)
    review.add_argument('--good',action='store_true')
    review.add_argument('--operator',required=True)
    withdraw=commands.add_parser('withdraw')
    withdraw.add_argument('target')
    withdraw.add_argument('--operator',required=True)
    alias=commands.add_parser('evidence')
    alias.add_argument('cut')
    alias.add_argument('anchors',type=Path)
    alias.add_argument('--confirmed-file',action='store_true')
    alias.add_argument('--operator',required=True)
    args=parser.parse_args()
    registry=Registry(args.data)
    if args.command=='report':
        result=report(args.data)
    elif args.command=='process':
        result=dict(linked=process(args.data))
    elif args.command=='export':
        result=bundle(args.data,args.destination)
    elif args.command=='admit':
        row=next((r for r in receipts(args.data) if r['id']==args.receipt),None)
        if not row:
            parser.error('Unknown receipt')
        result=dict(target=registry.admit(json.loads(row['payload']),confirmed_cut=args.confirmed_cut,
                                        software=args.software,operator=args.operator))
    elif args.command=='policy':
        registry.policy(args.actor,eligible=args.eligible,trusted=args.trusted,operator=args.operator)
        result=dict(updated=True)
    elif args.command=='review':
        registry.review(args.target,args.reviewer,args.good,operator=args.operator)
        result=dict(reviewed=True)
    elif args.command=='evidence':
        with args.anchors.open(encoding='utf8') as stream:
            raw=stream.read(4097)
        if len(raw)>4096:
            parser.error('Evidence input too large')
        identity=registry.add_evidence(args.cut,json.loads(raw),confirmed_file=args.confirmed_file,operator=args.operator)
        result=dict(evidence=identity)
    else:
        registry.withdraw(args.target,operator=args.operator)
        result=dict(withdrawn=True)
    print(json.dumps(result,sort_keys=True,indent=2))


if __name__=='__main__':
    main()
