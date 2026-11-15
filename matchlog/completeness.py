"""One definition of a complete settlement for the UI, queue and notices."""
from matchlog.recognition import nickname

FIELDS=('kills','assists','knocks','damage')

def summary_complete(summary, own_name=None, require_placement=True):
    summary=summary or {};players=summary.get('players') or []
    names=[nickname(p.get('name','')) for p in players]
    count=summary.get('expected_players',3)
    return ((not require_placement or summary.get('placement') is not None) and count in (1,2,3)
            and len(players)==count and len(set(names))==count
            and all(names) and (not own_name or nickname(own_name) in names)
            and all(all(isinstance(p.get(k),(int,float)) and p[k]>=0 for k in FIELDS) for p in players))

def page_complete(match, kind, own_name=None):
    if kind=='summary':return summary_complete(match.get('summary'),own_name)
    if kind=='rank':
        rank=match.get('rank') or {}
        return rank.get('delta') is not None and (not rank.get('tally_pending') or match.get('rp_manual')
                                                or rank.get('delta_source') in ('anchors','breakdown'))
    return False

def missing_fields(match, own_name=None):
    summary=match.get('summary') or {}
    players=summary.get('players') or []
    owner=nickname(own_name or '')
    own=next((p for p in players if not owner or nickname(p.get('name',''))==owner),{})
    missing=[]
    if match.get('record_source')=='last_snapshot' or not own:missing.append('结算')
    if summary.get('placement') is None:missing.append('排名')
    expected=summary.get('expected_players',3)
    if len(players)!=expected or any(any(p.get(k) is None for k in FIELDS) for p in players if p is not own):
        missing.append('队友数据')
    missing.extend(label for key,label in zip(FIELDS,('击杀','助攻','击倒','伤害')) if own.get(key) is None)
    mode=match.get('mode','ranked')
    if mode=='unknown' and not page_complete(match,'rank'):missing.append('模式')
    if mode=='ranked' and not page_complete(match,'rank'):missing.append('分数')
    return missing


def record_status(match, own_name=None):
    missing=missing_fields(match,own_name)
    if match.get('record_source')=='last_snapshot':
        return {'code':'snapshot','complete':False,'label':'最后快照','snapshot':True,'missing':[]}
    if missing or not summary_complete(match.get('summary'),own_name):
        return {'code':'incomplete','complete':False,'label':'待补全','missing':missing}
    return {'code':'recorded','complete':True,'label':'已修正' if match.get('corrected') else '已记录','missing':[]}
