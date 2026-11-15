"""Rank checkpoints align the total without inventing missing matches.

S31 division widths: https://help.ea.com/en/articles/apex-legends/ranked/
Checked 2026-11-05. Silver II is 750 in game, inferred also from the official
Gold IV reset (5750 minus 250 protection); the help page's 500 is inconsistent.
https://forums.ea.com/idea/apex-legends-bug-reports-en/silver-ii-requires-750-rp-instead-of-500-rp-to-rank-up-to-silver-i/13703655
An OCR capacity must match this table. Unknown seasons require a total.
"""
from collections.abc import Mapping
from datetime import datetime
import re
from typing import TypedDict

DIVISIONS=[]
base=0
for tier,widths in [('新秀',[250]*4),('青铜',[500]*4),('白银',[500,500,750,750]),
                    ('黄金',[750]*4),('白金',[750,750,1000,1000]),('钻石',[1000]*4)]:
    for division,width in zip(['IV','III','II','I'],widths):
        DIVISIONS.append((f'{tier} {division}',base,width));base+=width


class RankState(TypedDict, total=False):
    rank: str
    progress: int | float | None
    capacity: int | None
    next_rank: str
    total_rp: int | float


def stamp(value: str | int | float | None, fallback: float = 0) -> float:
    if value is None:return fallback
    try:return datetime.fromisoformat(value).timestamp() if isinstance(value,str) else float(value)
    except (ValueError,TypeError):return fallback


def rank_total(rank: Mapping[str, object] | None, season: str = 'S31') -> int | None:
    if not rank:return None
    explicit=rank.get('total_rp')
    if isinstance(explicit,(int,float)) and not isinstance(explicit,bool) and 0<=explicit<=2000000:return int(explicit)
    if rank.get('rank_anchor_status')=='ambiguous':return None
    if season!='S31':return None
    name=re.sub(r'\s+','',str(rank.get('rank') or '')).replace('铂金','白金')
    progress,capacity=rank.get('progress'),rank.get('capacity')
    for label,start,width in DIVISIONS:
        if name==label.replace(' ','') and isinstance(progress,int) and 0<=progress<width and capacity==width:
            return start+progress
    return None


def prefer_badge_anchor(first: Mapping[str, object], second: Mapping[str, object]) -> dict[str, object]:
    """Preserve a visually confirmed division across a weaker reread of the same card."""
    for verified, doubtful in ((first, second), (second, first)):
        if (verified.get('rank_anchor_status')=='badge' and verified.get('rank') and verified.get('next_rank')
                and doubtful.get('rank_anchor_status')=='ambiguous'
                and all(verified.get(field) is not None and verified.get(field)==doubtful.get(field)
                        for field in ('delta','progress','capacity'))):
            return {field:verified[field] for field in ('rank','next_rank','rank_anchor_status')}
    return {}


def rank_from_total(total: int | float | None, season: str = 'S31') -> RankState:
    if season!='S31' or total is None:return {}
    for i,(name,start,width) in enumerate(DIVISIONS):
        if start<=total<start+width:
            return {'rank':name,'progress':total-start,'capacity':width,
                    'next_rank':DIVISIONS[i+1][0] if i+1<len(DIVISIONS) else '大师','total_rp':total}
    return {'rank':'大师','total_rp':total,'progress':None,'capacity':None} if total>=16000 else {}


def build_progress(config, matches, checks, now=None):
    from time import time
    from matchlog.rp_reconcile import review, window_net
    def clean_rank(data):return {k:v for k,v in data.items() if k in ('rank','progress','capacity','next_rank','total_rp')}
    season=config['season'];start=stamp(config.get('initial_rp_at'))
    reviewed,anchors,gaps=review(config,matches,checks)
    by_id={row['match']['id']:row for row in reviewed}
    now=time() if now is None else now
    checkpoint_times=[stamp(c.get('captured_at'),stamp(c.get('created'))) for c in checks if rank_total(c,season) is not None]
    latest_anchor=max(checkpoint_times,default=None)
    events=[]
    if config.get('initial_rp') is not None:
        events.append({'kind':'initial','at':start,'value':config['initial_rp'],'label':'开始记录','source':'initial'})
    for m in matches:
        if m.get('mode','ranked')!='ranked':continue
        rank=m.get('rank') or {};row=by_id[m['id']];at=row['at']
        backfill=m.get('record_origin')=='upload' and not m.get('time_corrected')
        if backfill and latest_anchor is not None:at=min(at,latest_anchor-.001)
        events.append({'kind':'match','at':at,'match_id':m['id'],'delta':row['value'],
                       'absolute':rank_total(rank,season),'rank_data':rank,'source':'upload' if backfill else 'settlement','time_pending':backfill,
                       'label':'补录 · 时间待确认' if backfill else 'RP 待复核' if row['source']=='pending'
                       else 'RP 复核' if row['source']!='settlement'
                       else '最后快照' if m.get('record_source')=='last_snapshot' else '对局结算'})
    for check in checks:
        at=stamp(check.get('captured_at'),stamp(check.get('created')))
        value=rank_total(check,season)
        if value is None:continue
        events.append({'kind':'calibration','at':at,'absolute':value,'source':check.get('source','lobby'),
                       'label':check.get('label','大厅校准'),'rank_data':check})
    events.sort(key=lambda e:(e['at'],0 if e['kind']=='initial' else 1 if e['kind']=='match' else 2))
    # Backfilled games can predate the recording baseline. Work backwards from
    # the first real checkpoint, keeping missing deltas unknown.
    inferred={}
    first=next((i for i,e in enumerate(events) if e['kind']=='initial' or e.get('absolute') is not None),None)
    if first is not None:
        anchor=events[first];value=anchor.get('value',anchor.get('absolute'))
        if anchor['kind']=='match':value=value-anchor['delta'] if anchor.get('delta') is not None else None
        for i in range(first-1,-1,-1):
            event=events[i];inferred[i]=value
            value=value-event['delta'] if value is not None and event.get('delta') is not None else None
    points=[];total=None;latest_read=None;uncertain=False;last_absolute=None
    for i,e in enumerate(events):
        before=total
        if e['kind']=='initial':total=e['value']
        elif e['kind']=='match':
            delta=e.get('delta')
            estimate=inferred[i] if i in inferred else total+delta if total is not None and delta is not None else None
            if e['absolute'] is not None:
                total=e['absolute'];uncertain=False
                e['adjustment']=total-estimate if estimate is not None else None
                latest_read={**rank_from_total(total,season),**clean_rank(e['rank_data']),'total_rp':total,'at':e['at'],'source':e['source']}
                last_absolute=e['at']
            else:
                total=estimate;uncertain=uncertain or delta is None
        else:
            total=e['absolute'];e['adjustment']=total-before if before is not None else None
            uncertain=False;last_absolute=e['at']
            latest_read={**rank_from_total(total,season),**clean_rank(e['rank_data']),'total_rp':total,'at':e['at'],'source':e['source']}
            # Repeated identical lobby reads update freshness without cluttering the graph.
            if points and points[-1]['value']==total:continue
        points.append({k:v for k,v in {**e,'value':total,'uncertain':uncertain}.items() if k not in ('rank_data','absolute')})
    if latest_read:
        current={**latest_read,'estimated_total':total,'uncertain':uncertain,
                 'estimated':bool(points and last_absolute is not None and points[-1]['at']>last_absolute)}
        if current['estimated'] and total is not None:current.update(rank_from_total(total,season))
    elif total is not None:
        current={**rank_from_total(total,season),'total_rp':total,'at':points[-1]['at'],
                 'source':'initial','estimated':True,'estimated_total':total,'uncertain':uncertain}
    else:current=None
    return {'points':points,'current':current,'total_rp':total,'uncertain':uncertain,
            'recorded_delta':sum(r['value'] or 0 for r in reviewed),
            'recent_net':window_net(reviewed,anchors,now-43200,now),
            'recent_net_24':window_net(reviewed,anchors,now-86400,now),
            'review':{r['match']['id']:{k:r[k] for k in ('value','source','raw','estimate')}
                      for r in reviewed if r['source']!='settlement' or r['estimate']},
            'recent_gaps':[g for g in gaps if now-43200<=g['from_at'] and g['to_at']<=now]}
