"""Preview, apply and undo one explicit merge in the active profile."""

import json
from secrets import token_urlsafe
import time

from matchlog.capture_journal import settlement_keys
from matchlog.match_audit import archive_merged_record
from matchlog.recognition import nickname, summary_warnings


LINKS=(('capture_rounds','id'),('hud_backups','id'),('evidence','hash'))
UNDO='manual_merge_undo'
HUD_WARNING='局内快照的场次号可能误读。请核对两条记录的时间、队伍和原图，确认是同一局。'


def _pairs(store, ids):
    if (not isinstance(ids,(tuple,list)) or len(ids)!=2
            or not all(isinstance(ident,str) for ident in ids) or ids[0]==ids[1]):
        raise ValueError('请选择两条不同的记录')
    cfg=store.settings()
    rows={m['id']:m for m in store.all() if (m['season'],m['split'])==(cfg['season'],cfg['split'])}
    if any(ident not in rows for ident in ids):raise ValueError('只能合并当前档案和赛段的记录')
    return [rows[ident] for ident in ids]


def _compatible(store, first, second):
    if first.get('corrected') and second.get('corrected'):
        return '两条都已手动修正。请先核对修正内容。',None
    modes={m.get('mode') for m in (first,second)}&{'ranked','casual','event'}
    if len(modes)>1:return '两条记录的模式不同。',None
    first_confirmed,second_confirmed=(settlement_keys(m) for m in (first,second))
    if first_confirmed and second_confirmed and not first_confirmed&second_confirmed:
        return '两条记录各有不同场次的正式结算。',None
    first_keys,second_keys=(set(store.journal.game_keys(m['id'])) for m in (first,second))
    if first_keys and second_keys and not first_keys&second_keys:
        if (store.journal.overlapping_hud_fragment(first,second)
                or store.journal.overlapping_hud_fragment(second,first)):
            return None,HUD_WARNING
        return '两条有不同的场次标识。',None
    return None,None


def _conflicts(first, second):
    labels={'delta':'本局 RP','placement':'排名','kills':'击杀','assists':'助攻','knocks':'击倒','damage':'伤害','hero':'英雄'}
    def compare(a,b,keys,prefix=''):
        return [prefix+labels[k]+f'：{a[k]} / {b[k]}' for k in keys
                if a.get(k) is not None and b.get(k) is not None and a[k]!=b[k]]
    a,b=(m.get('summary') or {} for m in (first,second))
    found=compare(first.get('rank') or {},second.get('rank') or {},('delta',))+compare(a,b,('placement',))
    others={nickname(p['name']):p for p in b.get('players',[])}
    for p in a.get('players',[]):
        other=others.get(nickname(p['name']),{})
        found+=compare(p,other,('kills','assists','knocks','damage','hero'),p['name']+' ')
    if len({nickname(p['name']) for p in a.get('players',[])} | others.keys())>3:
        found.append('队员名单不同，合并后沿用主记录的队员。')
    if found:
        sources={first.get('record_source'),second.get('record_source')}
        if sources=={'last_snapshot','settlement'}:
            found.append('已结算数据优先于局内快照，手动修改保留。')
        else:
            found.append('冲突字段沿用主记录，其余缺项从另一条补齐。')
    return found


def _saved_rows(store, ids):
    return {ident:store.db.execute('SELECT created,payload FROM matches WHERE id=?',(ident,)).fetchone()
            for ident in ids}


def _unchanged(store, saved):
    for ident, current in _saved_rows(store, saved).items():
        if current is None or saved[ident] is None or current[0]!=saved[ident][0]:
            return False
        if current[1]==saved[ident][1]:
            continue
        before, after = json.loads(saved[ident][1]), json.loads(current[1])
        # Background completeness checks annotate rows without editing the match.
        before.pop('audit', None)
        after.pop('audit', None)
        if before != after:
            return False
    return True


def preview(store, ids):
    first,second=_pairs(store,ids)
    blocked,warning=_compatible(store,first,second)
    target=max((first,second),key=lambda m:(
        bool(m.get('corrected')), bool(store.journal.game_keys(m['id'])),
        m.get('record_source')=='settlement', (m.get('summary') or {}).get('placement') is not None,
        bool(m.get('rank'))))
    token=None if blocked else token_urlsafe(18)
    plan={'token':token,'rows':_saved_rows(store,ids),'scope':store.journal.scope(),'expires':time.monotonic()+180,
          'warning':warning,'keys':{ident:store.journal.game_keys(ident) for ident in ids}}
    return plan,{'token':token,'target_id':target['id'],'locked_target':target['id'] if target.get('corrected') else None,
                 'conflicts':[blocked] if blocked else _conflicts(first,second),
                 'warning':warning,'blocked':bool(blocked)}


def _complement(target,source):
    def fill(old,new,locked=()):
        return {**new,**{k:v for k,v in old.items() if v is not None or k in locked}}
    manual_fields=target.get('manual_fields',{})
    promote=(target.get('record_source')=='last_snapshot' and source.get('record_source')=='settlement'
             and (not target.get('corrected') or 'manual_fields' in target))
    for page in ('summary','rank'):
        if not source.get(page):continue
        if not target.get(page):
            target[page]=source[page]
            if source.get(page+'_captured_at'):target[page+'_captured_at']=source[page+'_captured_at']
            if page=='summary' and source.get('record_source'):target['record_source']=source['record_source']
            continue
        old,new=target[page],source[page]
        if page=='summary' and promote:
            # A retained record ID can still use the authoritative settlement
            # values, with explicitly edited fields kept from its snapshot.
            target['summary_captured_at']=source.get('summary_captured_at')
            target['record_source']='settlement'
            if source.get('round_id'):
                target['round_id']=source['round_id']
            if not target.get('time_corrected'):
                for field in ('created','time','settled_at','round_end_at','round_started_at','round_start_source'):
                    if source.get(field) is not None:
                        target[field]=source[field]
            old,new=new,old
        if page=='rank' and old.get('tally_pending') and not new.get('tally_pending') and not target.get('rp_manual'):
            old,new=new,old
        locked=('delta',) if page=='rank' and target.get('rp_manual') else ()
        if page=='summary' and manual_fields.get('placement'):
            locked=('placement',)
        target[page]=fill(old,new,locked)
        if page=='summary':
            if promote and locked:
                target[page]['placement']=new.get('placement')
            players={nickname(p['name']):p for p in old.get('players',[])}
            incoming={nickname(p['name']):p for p in new.get('players',[])}
            if len(players.keys()|incoming.keys())<=3:
                locked_players=manual_fields.get('players',{})
                target[page]['players']=[
                    fill(players.get(n,{}),incoming.get(n,{}),locked_players.get(n,()))
                    for n in dict.fromkeys((*players,*incoming))]
                if promote:
                    for player in target[page]['players']:
                        name=nickname(player['name'])
                        for field in locked_players.get(name,()):
                            player[field]=incoming.get(name,{}).get(field)
            target[page]['warnings']=summary_warnings(target[page])
    if target.get('mode') in ('unknown',None) and source.get('mode') in ('ranked','casual','event'):
        target.update(mode=source['mode'],mode_source='manual_merge')
    for field in ('round_id','round_started_at','round_start_source','settled_at','round_end_at','map_name',
                  'snapshot_at','snapshot_backup_id','snapshot_live_rp','_snapshot_created'):
        if target.get(field) is None and source.get(field) is not None:target[field]=source[field]


def confirm(store, plan, token, target_id, acknowledged=False):
    with store.lock, store.db:
        store.db.execute('BEGIN IMMEDIATE')
        if (not plan or not token or token!=plan['token'] or time.monotonic()>plan['expires']
                or plan['scope']!=store.journal.scope() or target_id not in plan['rows']):
            raise ValueError('预览已失效，请重新核对')
        first,second=_pairs(store,list(plan['rows']))
        if (not _unchanged(store, plan['rows']) or
                any(store.journal.game_keys(ident)!=keys for ident,keys in plan['keys'].items())):
            raise ValueError('记录已更新，请重新核对')
        source,target=(second,first) if first['id']==target_id else (first,second)
        problem,warning=_compatible(store,source,target)
        if problem:
            raise ValueError(problem)
        if warning!=plan['warning']:
            raise ValueError('场次证据已变化，请重新核对')
        if warning and acknowledged is not True:
            raise ValueError('请核对两条记录并确认场次号冲突')
        if source.get('corrected'):
            raise ValueError('请选择手动修正过的记录作为主记录')
        source_id=source['id']
        links={table:{ident:[r[0] for r in store.db.execute(
            f'SELECT {key} FROM {table} WHERE match_id=? ORDER BY {key}',(ident,))]
            for ident in (source_id,target_id)} for table,key in LINKS}
        before=_saved_rows(store, plan['rows'])
        _complement(target,source)
        archive_merged_record(store,source,target,'手动合并',preserve_highlights=True)
        store._save(target,commit=False)
        undo={'scope':plan['scope'],'before':before,'after':_saved_rows(store,plan['rows']),
              'source':source_id,'target':target_id,'links':links}
        store.db.execute('INSERT OR REPLACE INTO recorder_flags VALUES (?,?)',(UNDO,json.dumps(undo,ensure_ascii=False)))
    if store.active_id==source_id:
        store.active_id=target_id


def undo_last(store):
    with store.lock, store.db:
        store.db.execute('BEGIN IMMEDIATE')
        row=store.db.execute('SELECT value FROM recorder_flags WHERE name=?',(UNDO,)).fetchone()
        if not row:
            raise ValueError('没有可撤销的合并')
        undo=json.loads(row[0])
        source,target=undo['source'],undo['target']
        if undo['scope']!=store.journal.scope():
            raise ValueError('请切回合并时的档案和赛段')
        if not _unchanged(store, undo['after']):
            raise ValueError('合并后的记录已被修改，无法安全撤销')
        for table,key in LINKS:
            links=undo['links'][table]
            current={r[0] for r in store.db.execute(f'SELECT {key} FROM {table} WHERE match_id=?',(target,))}
            if current!=set(links[source])|set(links[target]):
                raise ValueError('合并后的截图或场次关联已变化，无法安全撤销')
            store.db.executemany(f'UPDATE {table} SET match_id=? WHERE {key}=?',((source,ident) for ident in links[source]))
        store.db.executemany('UPDATE matches SET created=?,payload=? WHERE id=?',
                             ((*row,ident) for ident,row in undo['before'].items()))
        store.db.execute('DELETE FROM recorder_flags WHERE name=?',(UNDO,))
    if store.active_id in (source,target):
        store.active_id=None
