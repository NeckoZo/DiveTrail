"""Recent-record inspection and evidence-only recovery. Missing values remain unknown."""
from pathlib import Path
from datetime import datetime,timezone
import hashlib,json,time
from matchlog.capture_journal import _review_retry_key
from matchlog.completeness import FIELDS, missing_fields, page_complete, summary_complete
from matchlog.recognition import nickname
from matchlog.media_paths import existing_media_path, media_dirs
from matchlog.profile_rename import profile_name_matches, relabel_historical_result

def inspect_recent(store, now=None, hours=24):
    from matchlog.rp_reconcile import apply_review
    now=time.time() if now is None else now
    issues=[]
    with store.lock:
        cfg=store.settings()
        matches=store.all()
        reviewed=store.rank_progress(matches)['review']
        with store.db:
            for match in matches:
                if now-hours*3600<=match['created']<=now and (match['season'],match['split'])==(cfg['season'],cfg['split']):
                    missing=missing_fields(apply_review(match,reviewed.get(match['id'])),cfg['self_name'])
                    match['audit']={'checked_at':now,'missing':missing,'state':'missing' if missing else 'complete'}
                    store._save(match,commit=False)
                    if missing:issues.append({'id':match['id'],'missing':missing})
        snapshots=store.db.execute('SELECT created,payload FROM profile_snapshots WHERE scope=? AND created<=? ORDER BY created',(nickname(cfg['self_name']),now)).fetchall()
    # A profile count is a cross-check, never a reason to invent or discard a match.
    since=now-12*3600;profiles=[]
    for at,raw in snapshots:
        p=json.loads(raw)
        if p.get('selected_season')==cfg['season'] and (p.get('season') or {}).get('matches') is not None:profiles.append((at,p))
    before=[r for r in profiles if r[0]<=since];after=[r for r in profiles if r[0]>since]
    expected=compared=None
    if before and after and before[-1][1].get('mode_label')==after[-1][1].get('mode_label'):
        expected=after[-1][1]['season']['matches']-before[-1][1]['season']['matches']
        compared=sum(before[-1][0]<m['created']<=after[-1][0] and m['season']==cfg['season'] and m.get('mode')!='event' for m in matches)
    actual=sum(since<=m['created']<=now and m['season']==cfg['season'] for m in matches)
    return {'checked_at':now,'hours':hours,'issues':issues,'recent_count':actual,'profile_count':expected,'profile_local_count':compared,'count_mismatch':expected is not None and compared!=expected}

def _needed_pages(match, own, reviewed):
    missing=set(missing_fields(match,own))
    needed=set()
    if missing-{'模式','分数'}:needed.add('summary')
    if '模式' in missing or ('分数' in missing and reviewed.get(match['id'],{}).get('value') is None):
        needed.add('rank')
    if '排名' in missing and match.get('mode')=='ranked' and (match.get('rank') or {}).get('placement') is None:
        needed.add('rank')
    return needed


def _repair_sources(store, match_id=None):
    """Order the archived evidence and draft queue on one capture timeline."""
    with store.lock:
        cfg=store.settings()
        matches={m['id']:m for m in store.all() if (m['season'],m['split'])==(cfg['season'],cfg['split']) and m.get('round_id')}
        reviewed=store.rank_progress(list(matches.values()))['review'] if matches else {}
        # Snapshot rows before releasing the shared SQLite connection. Compute
        # retry keys after the query so SQLite never calls back into Python.
        jobs=store.db.execute("""SELECT j.created,j.path,j.id,j.kind,j.payload,r.match_id,j.round_id
            FROM capture_jobs j JOIN capture_rounds r ON r.id=j.round_id AND r.scope=j.scope
            WHERE j.scope=? AND r.discarded=0
              AND (j.status IN ('review','failed') OR (? IS NOT NULL AND j.status IN ('pending','done','archived')))
              AND (? IS NULL OR r.match_id=?)""",(store.journal.scope(),match_id,match_id,match_id)).fetchall()
        retries=dict(store.db.execute("SELECT key,attempts FROM recognition_retries WHERE key GLOB 'v2:job:*'"))
    needed={mid:_needed_pages(match,cfg['self_name'],reviewed) for mid,match in matches.items()}
    sources=[]
    for mid,match in matches.items():
        if match_id is not None and mid!=match_id:continue
        for evidence in match.get('evidence',[]):
            kind=evidence['kind']
            if kind not in needed[mid]:continue
            at=match.get(kind+'_captured_at') or match.get('time')
            at=datetime.fromisoformat(at).timestamp() if at else match['created']
            sources.append((at,kind,evidence['path'],mid,None,None,0))
    needs_rp=any('rank' in pages for pages in needed.values())
    for at,path,ident,kind,raw,mid,rid in jobs:
        if mid and not needed.get(mid):continue
        key=_review_retry_key(path,rid)
        attempts=retries.get(key,0)
        if attempts>=2 and raw is None:continue
        parsed_kind=(json.loads(raw).get('kind') if raw else None)
        if kind in ('settlement','manual','probe'):
            if parsed_kind in ('rank','summary') and mid and parsed_kind not in needed[mid]:continue
            if parsed_kind in ('postmatch','rank_overview','death_recap','training','hud','lobby','profile','intro'):
                continue
            expected=parsed_kind if parsed_kind in ('rank','summary') else None
        elif kind=='hud' and mid and 'summary' in needed[mid] and attempts<2:
            expected=kind
        elif kind=='lobby' and needs_rp and attempts<2 and match_id is None:
            expected=kind
        else:continue
        sources.append((at,expected,path,mid,ident,key,attempts))
    return matches,needed,sorted(sources,key=lambda source:(source[0],source[4] is not None),reverse=True)


def _fills_missing(store, match, result, own, at):
    """Keep previously read values, including a verified zero, when combining pages."""
    kind=result.get('kind')
    key=store.journal.key_for_round(match['round_id'])
    old=match.get(kind) or {}
    if result.get('game_key') and any(known and known!=result['game_key'] for known in (key,old.get('game_key'))):
        return None
    placement=result.get('placement')
    if placement is not None and any(p.get('placement') not in (None,placement)
                                     for p in (match.get('summary') or {},match.get('rank') or {})):
        return None
    if kind=='rank':
        if result.get('delta') is None or result.get('tally_pending') or match.get('mode') in ('casual','event'):return None
        if not page_complete(match,'rank') or (old.get('placement') is None and placement is not None):
            return result
        return None
    if kind!='summary':return None
    result=relabel_historical_result(store,result,match['round_id'],at)
    result=with_rank_placement(store,result,match['round_id'])
    fresh={nickname(p.get('name','')):p for p in result.get('players',[])}
    known={nickname(p.get('name','')):p for p in old.get('players',[])}
    if nickname(own) not in fresh or '' in fresh or len(fresh)!=len(result['players']):return None
    if match.get('record_source')=='last_snapshot':
        return result if summary_complete(result,own) else None
    if len(known.keys()|fresh.keys())>3:return None
    useful=old.get('placement') is None and result.get('placement') is not None
    useful=useful or any(name not in known or any(known[name].get(f) is None and player.get(f) is not None for f in FIELDS)
                         for name,player in fresh.items())
    if useful:
        return {**result,'players':[*fresh.values(),*(p for name,p in known.items() if name not in fresh)]}
    return None


def _repair_source(store, ocr, source, matches, needed, seen, run_aux, report):
    at,expected,value,mid,ident,key,attempts=source
    job=store.journal.get(ident) if ident else None
    path=existing_media_path(store.root,value)
    if not path:
        if ident:store.journal.finish(ident,'dismissed','原图已不存在')
        if report:report('repair_source_missing',job,mid)
        return False,False
    match=matches.get(mid)
    rid=(match or job or {}).get('round_id')
    if not rid:return False,False
    own=store.settings()['self_name']

    def apply(result):
        kind=result.get('kind')
        if kind not in ('rank','summary') or (expected and expected!=kind):return False
        if not match:return bool(job and recover_orphan(store,ocr,job,recognized=result))
        if kind not in needed[mid]:return False
        if job and job['round_id']!=rid:
            if store.journal.settlement_alias(job['round_id'],result,at)!=rid:return False
        candidate=_fills_missing(store,match,result,own,at)
        if not candidate:return False
        saved=fill_from_image(store,candidate,path,rid,at)
        if saved!=mid:return False
        current=next((m for m in store.all() if m['id']==mid),match)
        if current.get(kind)==match.get(kind):return False
        if page_complete(current,kind,own):store.journal.resolve_page(rid,kind)
        return True

    observation={k:v for k,v in ((job or {}).get('payload') or {}).items() if not k.startswith('_')}
    if observation and expected in ('rank','summary'):
        try:
            if apply(observation):
                if report:report('repair_observation',job,mid)
                return True,True
        except ValueError:
            if report:report('repair_conflict',job,mid)
    if attempts>=2 or (str(path),rid) in seen:return False,False
    if expected in ('hud','lobby') and not run_aux:return False,False
    seen.add((str(path),rid))
    if key is None:
        key='v2:'+mid+':'+expected+':'+hashlib.sha256(path.read_bytes()).hexdigest()
    with store.lock,store.db:
        row=store.db.execute('SELECT attempts FROM recognition_retries WHERE key=?',(key,)).fetchone()
        attempts=row[0] if row else 0
        if attempts>=2:return False,False
        store.db.execute('INSERT OR REPLACE INTO recognition_retries VALUES (?,?)',(key,attempts+1))
    if expected in ('hud','lobby'):
        run_aux(job)
        return True,False
    try:
        result=ocr.read(path)
        saved=apply(result)
        if report:report('repair_ocr' if saved else 'repair_no_change',job,mid)
        return True,saved
    except ValueError:
        if report:report('repair_conflict',job,mid)
        return True,False


def repair_incomplete(store, ocr, *, run_aux=None, report=None):
    """Make one useful repair attempt, newest capture first across the active split."""
    matches,needed,sources=_repair_sources(store)
    seen=set()
    for source in sources:
        attempted,_=_repair_source(store,ocr,source,matches,needed,seen,run_aux,report)
        if attempted:return True
    return False


def recover_match(store, ocr, match_id):
    recovered=0
    seen=set()
    for _ in range(12):
        matches,needed,sources=_repair_sources(store,match_id)
        for source in sources:
            attempted,saved=_repair_source(store,ocr,source,matches,needed,seen,None,None)
            if attempted:
                recovered+=int(saved)
                break
        else:break
    return recovered

def with_rank_placement(store, result, round_id):
    """Use a confirmed rank page only when the incoming summary names the exact same session."""
    from matchlog.completeness import page_complete
    key=result.get('game_key')
    if not round_id or result.get('kind')!='summary' or result.get('placement') is not None or not key:
        return result
    with store.lock:
        row=store.db.execute("""SELECT m.payload FROM capture_rounds r JOIN matches m ON m.id=r.match_id
            WHERE r.id=? AND r.scope=? AND r.game_key=? AND r.discarded=0
              AND json_extract(m.payload,'$.account')=json_extract(r.scope,'$[2]')""",
            (round_id,store.journal.scope(),key)).fetchone()
    match=json.loads(row[0]) if row else {}
    rank=match.get('rank') or {}
    placement=rank.get('placement')
    if (type(placement) is int and 1<=placement<=30 and not match.get('deleted_at')
            and not match.get('merged_into') and rank.get('game_key') in (None,key) and page_complete(match,'rank')):
        return {**result,'placement':placement}
    return result

def recover_observed_summary(store, round_id=None, match_id=None):
    """Fill a settled record from the best already-read frame, without a second OCR pass."""
    with store.lock:
        cfg=store.settings();scope=store.journal.scope()
        row=store.db.execute("""SELECT m.payload FROM capture_rounds r JOIN matches m ON m.id=r.match_id
            WHERE r.scope=? AND r.discarded=0 AND (r.id=? OR r.match_id=?)""",
            (scope,round_id,match_id)).fetchone()
        if not row:return None
        match=json.loads(row[0]);canonical=match.get('round_id');old=match.get('summary')
        if (not canonical or match.get('deleted_at') or match.get('merged_into')
                or match.get('account')!=nickname(cfg['self_name'])
                or (match.get('season'),match.get('split'))!=(cfg['season'],cfg['split'])
                or (match.get('record_source')!='last_snapshot' and summary_complete(old,cfg['self_name']))):
            return None
        key=store.db.execute('SELECT game_key FROM capture_rounds WHERE id=? AND scope=? AND discarded=0',
                             (canonical,scope)).fetchone()
        if not key or not key[0]:return None
        jobs=store.db.execute("""SELECT id,created,path,payload FROM capture_jobs
            WHERE scope=? AND round_id=? AND status IN ('review','failed')
              AND json_extract(payload,'$.kind')='summary' AND json_extract(payload,'$.game_key')=?
            ORDER BY created DESC LIMIT 8""",(scope,canonical,key[0])).fetchall()
    for ident,at,path,raw in jobs:
        source=existing_media_path(store.root,path)
        if not source:continue
        result=json.loads(raw);result.pop('_origin_round_id',None)
        result=_fills_missing(store,match,result,cfg['self_name'],at)
        if not result:continue
        rank=match.get('rank') or {}
        if rank.get('game_key') not in (None,key[0]):continue
        rank_placement=rank.get('placement') if page_complete(match,'rank') else None
        if rank_placement is not None and result.get('placement') not in (None,rank_placement):continue
        if (not old or match.get('record_source')=='last_snapshot') and rank_placement is None:continue
        try:
            saved=fill_from_image(store,result,source,canonical,at)
        except (OSError,ValueError):
            continue
        if saved:
            current=next((m for m in store.all() if m['id']==saved),{})
            if page_complete(current,'summary',cfg['self_name']):
                store.journal.finish(ident,'done')
                return saved
            match=current
    return None

def fill_from_image(store,result,path,round_id,at):
    from matchlog.image_storage import archive_settlement
    path=Path(path).resolve()
    archived=path.parent in (*media_dirs(store.root,result['kind']),*media_dirs(store.root,'captures'))
    saved,created=(path,False) if archived else archive_settlement(store,result['kind'],path.read_bytes(),at)
    try:
        ident=store.ingest(with_rank_placement(store,result,round_id),saved,timestamp=at,round_id=round_id,fill_only=True)
        if ident and result.get('kind') in ('summary','rank'):
            from matchlog.highlights import sync_highlight
            sync_highlight(store,ident)
        return ident
    finally:
        if created and not any(Path(e['path']).resolve()==saved for m in store.all() for e in m['evidence']):saved.unlink(missing_ok=True)

def recover_orphan(store,ocr,job,*,recognized=None):
    path=existing_media_path(store.root,job['path']);cfg=store.settings()
    if not path or job['scope']!=store.journal.scope():return False
    parsed=recognized if recognized is not None else ocr.read(path)
    kind=parsed.get('kind');key=parsed.get('game_key')
    if kind not in ('rank','summary'):return False
    original=job['round_id']
    if kind=='summary':
        parsed=relabel_historical_result(store,parsed,original,job['created'])
    if kind=='summary' and not summary_complete(parsed,cfg['self_name'],require_placement=False):return False
    if kind=='rank' and (parsed.get('tally_pending') or parsed.get('delta') is None):return False
    if not original:return False
    if key:
        if kind=='rank' and not store.journal.match_for_game_key(key):return False
        rid=store.journal.bind_game_key(original,key,job['created'],'settlement',job['id'],allow_alias=False,observation=parsed)
        if not rid:return False
        canonical=store.journal.key_for_round(rid)
        if canonical and canonical!=key:parsed={**parsed,'game_key':canonical}
        provisional=(job.get('payload') or {}).get('_origin_round_id') or original
        store.journal.reconcile_settlement(provisional,rid,parsed,job['created'])
    else:
        rid=store.journal.settlement_alias(original,parsed,job['created'])
        if not rid or rid==original:return False
    if kind=='rank':
        with store.lock:
            if not store.db.execute('SELECT 1 FROM capture_rounds WHERE id=? AND match_id IS NOT NULL',(rid,)).fetchone():return False
    ident=fill_from_image(store,parsed,path,rid,job['created'])
    match=next((m for m in store.all() if m['id']==ident),{})
    if page_complete(match,kind,cfg['self_name']):store.journal.finish(job['id'],'done');return True
    return False

def recover_profile_fields(store,ocr):
    with store.lock:
        own=nickname(store.settings()['self_name'])
        row=store.db.execute('SELECT id,payload,path,created FROM profile_snapshots WHERE scope=? ORDER BY created DESC LIMIT 1',(own,)).fetchone()
    path=existing_media_path(store.root,row[2]) if row else None
    if not path:return 0
    old=json.loads(row[1])
    if all((old.get(s) or {}).get(k) is not None for s in ('lifetime','season') for k in ('matches','wins','top5','damage','kills','highest_kills')):return 0
    fresh=ocr.read(path)
    if fresh.get('kind')!='profile' or not profile_name_matches(fresh,old,row[3],own):return 0
    count=0
    for section in ('lifetime','season'):
        if section=='season' and old.get('selected_season')!=fresh.get('selected_season'):continue
        dst=old.setdefault(section,{})
        for key,value in (fresh.get(section) or {}).items():
            if key in ('display_values','approximate_fields') or dst.get(key) is not None or value is None:continue
            dst[key]=value;count+=1
            shown=(fresh[section].get('display_values') or {}).get(key)
            if shown:dst.setdefault('display_values',{})[key]=shown
    if count:
        with store.lock,store.db:
            return count if store.db.execute('UPDATE profile_snapshots SET payload=? WHERE id=? AND scope=? AND payload=?',
                (json.dumps(old,ensure_ascii=False),row[0],own,row[1])).rowcount else 0
    return count

def merge_records(store, source_id, target_id, reason, *, snapshot=False):
    """Archive the duplicate row and keep its evidence and user corrections accessible."""
    with store.lock:
        records={m['id']:m for m in store.all()}
        if source_id==target_id or source_id not in records or target_id not in records:return False
        source,target=records[source_id],records[target_id]
        if (source['season'],source['split'])!=(target['season'],target['split']):raise ValueError('不能合并不同赛段')
        if source.get('corrected'):raise ValueError('已修改的记录须保留为主记录')
        if target.get('mode') in (None,'unknown') and source.get('mode') in ('ranked','casual','event'):
            target.update(mode=source['mode'],mode_source='verified_merge')
        with store.db:
            if snapshot:
                from matchlog.record_merge import _complement
                _complement(target,source)
            archive_merged_record(store,source,target,reason,preserve_highlights=True)
            store._save(target,commit=False)
            if snapshot and source.get('round_id') and target.get('round_id')!=source['round_id']:
                store.db.execute("""UPDATE capture_rounds SET phase='alias'
                    WHERE id=? AND match_id=?""",(source['round_id'],target_id))
        if snapshot and target.get('round_id') and hasattr(store.journal,'trim_hud'):
            store.journal.trim_hud(target['round_id'])
        return True


def merge_repeated_rank_page(store, match_id):
    """Merge a rank-only fragment only if a nearly simultaneous rank page verifies it."""
    def fingerprint(match):
        rank=match.get('rank') or {}
        if (match.get('corrected') or match.get('record_origin') or match.get('rp_manual')
                or rank.get('tally_pending') or rank.get('game_key')):
            return None
        fields=('delta','placement','progress','capacity')
        details=rank.get('breakdown') or {}
        parts=('combat_rp','bonus_rp','placement_rp','entry_cost')
        if not rank.get('rank') or not all(type(rank.get(key)) is int for key in fields):
            return None
        if not all(type(details.get(key)) is int for key in parts):
            return None
        return (rank['rank'],*(rank[key] for key in fields),*(details[key] for key in parts))

    with store.lock:
        records=store.all()
        current=next((record for record in records if record['id']==match_id),None)
        if not current or not (signature:=fingerprint(current)) or not current.get('rank_captured_at'):
            return match_id
        at=datetime.fromisoformat(current['rank_captured_at']).timestamp()
        candidates=[record for record in records if record['id']!=match_id and record.get('rank_captured_at')
                    and (record['season'],record['split'])==(current['season'],current['split'])
                    and abs(datetime.fromisoformat(record['rank_captured_at']).timestamp()-at)<=8
                    and bool(record.get('summary'))!=bool(current.get('summary'))
                    and fingerprint(record)==signature]
        if len(candidates)!=1:
            return match_id
        source,target=(candidates[0],current) if current.get('summary') else (current,candidates[0])
        if target.get('record_source')!='settlement' or target['summary'].get('placement')!=target['rank']['placement']:
            return match_id
        if merge_records(store,source['id'],target['id'],'短时间内重复读到相同的排位结算页'):
            return target['id']
        return match_id


def merge_repeated_snapshot(store, match_id):
    """Only consolidate an unedited HUD fragment when combat and roster agree exactly."""
    with store.lock:
        rows=store.all()
        final=next((row for row in rows if row['id']==match_id),None)
        if not final or final.get('record_source')!='settlement' or not final.get('summary_captured_at'):return False
        own=nickname(store.settings()['self_name'])
        at=datetime.fromisoformat(final['summary_captured_at']).timestamp()
        keys=store.journal.game_keys(match_id)
        def fingerprint(record):
            players=(record.get('summary') or {}).get('players',[])
            names={nickname(p.get('name','')) for p in players}
            p=next((p for p in players if nickname(p.get('name',''))==own),{})
            values=tuple(p.get(key) for key in ('kills','assists','damage'))
            if len(names)==3 and all(names) and all(type(v) is int for v in values) and values[-1]>0:
                return names,values
            return None
        expected=fingerprint(final)
        if not expected:return False
        candidates=[]
        for record in rows:
            if (record.get('record_source')!='last_snapshot' or record.get('corrected') or record.get('rank')
                    or record.get('record_origin') or not record.get('snapshot_at')
                    or any(record.get(k)!=final.get(k) for k in ('season','split','mode'))):continue
            first=datetime.fromisoformat(record['snapshot_at']).timestamp()
            if not 0<=at-first<=180 or fingerprint(record)!=expected:continue
            existing=store.journal.game_keys(record['id'])
            if keys and existing and not keys&existing and not store.journal.overlapping_hud_fragment(record,final):
                continue
            if store.db.execute("""SELECT 1 FROM capture_rounds r WHERE r.scope=? AND r.started>? AND r.started<=?
                AND r.id NOT IN (?,?) AND r.discarded=0 AND r.phase NOT IN ('upload','alias')
                AND COALESCE(r.match_id,'') NOT IN (?,?)
                AND (r.game_key IS NOT NULL OR r.phase='intro' OR EXISTS (
                SELECT 1 FROM matches m WHERE m.id=r.match_id AND json_extract(m.payload,'$.round_started_at') IS NOT NULL))
                LIMIT 1""",(store.journal.scope(),first,at,record.get('round_id') or '',final.get('round_id') or '',
                             record['id'],final['id'])).fetchone():continue
            candidates.append(record['id'])
        return len(candidates)==1 and merge_records(store,candidates[0],match_id,'同一队伍的结算与最后快照',snapshot=True)


def archive_merged_record(store, source, target, reason, *, preserve_highlights=False):
    """Archive a verified duplicate within the caller's transaction."""
    source_id,target_id=source['id'],target['id']
    target.setdefault('merged_records',[]).append({'id':source_id,'time':source['time'],'reason':reason})
    for evidence in source.get('evidence',[]):
        if evidence not in target['evidence']:target['evidence'].append(evidence)
    source.update(merged_into=target_id,merge_reason=reason)
    store._save(source,commit=False)
    for table in ('capture_rounds','hud_backups','evidence'):
        store.db.execute(f'UPDATE {table} SET match_id=? WHERE match_id=?',(target_id,source_id))
    collected = preserve_highlights and store.db.execute(
        'SELECT 1 FROM match_highlights WHERE match_id IN (?,?)', (source_id, target_id)).fetchone()
    if collected or target.get('preserve_merged_highlights') or source.get('preserve_merged_highlights'):
        target['preserve_merged_highlights']=True
    else:
        store.db.execute('UPDATE OR IGNORE match_highlights SET match_id=? WHERE match_id=?',(target_id,source_id))
