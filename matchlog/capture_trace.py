"""Typed per-job diagnostics shared by the local timeline and support export."""

from datetime import datetime
import json
import time

from matchlog.capture_diagnostics import CHINA, PHASES

OUTCOMES={
    'saved':'本页入档','partial':'已补入部分字段','auxiliary':'已保存快照或资料','ignored':'已跳过',
    'unchanged':'已有同等或更新的数据','review':'待核对','failed':'处理失败',
}
REASONS={
    'wrong_scope':'档案或赛段已切换','duplicate':'同场同页的重复帧','identity_unconfirmed':'未确认本人',
    'tally_pending':'计分动画或明细待核对','unstable':'等待独立画面复核','missing_rp':'未读取到净 RP',
    'overview_only':'排位概览，等待经验值页','unknown':'未确认页面','record_blocked':'场次关联未确认',
    'source_unavailable':'原图已清理或不可读','recognition_error':'识别或入库失败',
    'complete_already':'该页已有同等或更新的证据','repair_conflict':'补录与已有数据冲突',
    'repair_observation':'复用了已读数据','repair_ocr':'补录重读原图','repair_no_change':'原图未提供新字段',
    'repair_source_missing':'补录时原图已不存在','navigation_only':'导航页','not_required':'已有足够证据',
}
ROUTES=('rank_crop','rank_regions','full_frame','full','full_smaller','hud','lobby','cached','probe','manual','profile','intro')


def size_pair(value):
    if isinstance(value,(tuple,list)) and len(value)==2 and all(type(n) is int and 0<n<=32768 for n in value):
        return list(value)
    return None


def record(store,job,*,round_id=None,page=None,outcome,reason=None,started=None,ocr_ms=None,
           input_px=None,client_px=None,route=None,queue_depth=None,scope_mode=None):
    if outcome not in OUTCOMES:return
    now=time.time()
    payload={'page':page if page in PHASES else None,'outcome':outcome,
             'reason':reason if reason in REASONS else None,'route':route if route in ROUTES else None,
             'input_px':size_pair(input_px),'client_px':size_pair(client_px or (job.get('payload') or {}).get('_client_px')),
             'queue_depth':queue_depth if type(queue_depth) is int and 0<=queue_depth<=10000 else None,
             'queued_ms':round(max(0,started-job['created'])*1000) if started else None,
             'ocr_ms':round(max(0,ocr_ms)) if ocr_ms is not None else None,
             'process_ms':round(max(0,now-started)*1000) if started else None,
             'game_priority':bool(scope_mode)}
    with store.lock,store.db:
        store.db.execute('INSERT OR REPLACE INTO capture_trace VALUES (?,?,?,?,?)',
                         (job['id'],round_id or job.get('round_id'),job.get('round_id'),now,json.dumps(payload)))


def job_trace(store,job_id):
    row=store.db.execute('SELECT origin_round_id,round_id,finished,payload FROM capture_trace WHERE job_id=?',
                         (job_id,)).fetchone()
    if not row:return None
    origin,rid,finished,raw=row
    return {**json.loads(raw),'origin_round_id':origin,'round_id':rid,'finished':finished}


def timeline(store,match_id):
    from matchlog.completeness import record_status
    with store.lock:
        match=next((item for item in store.all() if item['id']==match_id),None)
        if not match:raise ValueError('对局不存在')
        status=record_status(match,store.settings()['self_name'])
        rows=store.db.execute("""SELECT j.created,j.kind,j.status,j.payload,t.payload,j.id
            FROM capture_jobs j LEFT JOIN capture_trace t ON t.job_id=j.id
            WHERE EXISTS (SELECT 1 FROM capture_rounds r WHERE r.match_id=? AND r.id IN
              (j.round_id,t.round_id,t.origin_round_id,json_extract(j.payload,'$._origin_round_id')))
            ORDER BY j.created DESC LIMIT 100""",(match_id,)).fetchall()
    title=['本局采集经过',f"{match['time'][:16].replace('T',' ')}  {status['label']}"]
    if status['missing']:title.append('待补：'+'、'.join(status['missing']))
    for at,kind,job_status,raw,trace,_ in reversed(rows):
        observation=json.loads(raw) if raw else {}
        data=json.loads(trace) if trace else {}
        page=data.get('page') or observation.get('kind') or kind
        label=OUTCOMES.get(data.get('outcome')) or {
            'done':'任务已结束','review':'待核对','failed':'处理失败','archived':'已归档',
            'dismissed':'线索已清理','deferred':'延后识别','pending':'待识别','processing':'识别中',
        }.get(job_status,'已留线索')
        items=[PHASES.get(page,'截图'),label,REASONS.get(data.get('reason'))]
        for key,field in (('queued_ms','排队'),('ocr_ms','OCR'),('process_ms','处理')):
            if data.get(key) is not None:items.append(f"{field} {data[key]/1000:.2f}s")
        title.append(datetime.fromtimestamp(at,CHINA).strftime('%H:%M:%S')+'  '+' · '.join(filter(None,items)))
    if not rows:title.append('这局没有留存的采集事件。')
    return '\n'.join(title)
