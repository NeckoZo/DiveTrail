"""Durable capture queue. A saved frame is evidence, never an automatic match."""
from pathlib import Path
from datetime import datetime
import hashlib
import json
import re
import time
import uuid
from matchlog.recognition import nickname
from matchlog.media_paths import existing_media_path, media_dir


SETTLEMENT_PAIR_SECONDS = 90
OTHER_SETTLEMENT_PAGE = {'summary':'rank','rank':'summary'}
HUD_DRAFT_LIMIT = 4
HUD_READ_LIMIT = 2
HUD_FILE_LIMIT = 3


def queued_frame_size(size, kind):
    width,height=size
    if kind in ('settlement','manual','probe','profile') and width>1920:
        return 1920,round(height*1920/width)
    return size


def queued_frame(image, kind):
    size=queued_frame_size(image.size,kind)
    if size==image.size:return image
    from PIL import Image
    return image.resize(size,Image.Resampling.LANCZOS)


def frame_asset(image):
    return hashlib.sha256(str(image.size).encode()+image.tobytes()).hexdigest()[:24]


def _review_retry_key(path, round_id):
    return 'v2:job:'+hashlib.sha256((str(path)+str(round_id)).encode()).hexdigest()


def _same_settlement(first, second, own_name):
    kind=first.get('kind')
    if kind!=second.get('kind') or first.get('placement') is None or first['placement']!=second.get('placement'):
        return False
    if kind=='rank':
        return not (first.get('tally_pending') or second.get('tally_pending')) and all(
            type(first.get(field)) is int and first[field]==second.get(field)
            for field in ('delta','progress','capacity'))
    if kind!='summary':return False
    def roster(page):
        return {nickname(p.get('name','')):p for p in page.get('players',[])}
    left,right=roster(first),roster(second)
    owner=nickname(own_name)
    if len(left)!=3 or left.keys()!=right.keys() or '' in left or owner not in left:return False
    return all(type(left[owner].get(field)) is int and left[owner][field]==right[owner].get(field)
               for field in ('kills','assists','damage'))


def settlement_keys(record):
    """Keys saved with official pages are stronger than a round's HUD footer."""
    keys=set()
    for page in ('summary','rank'):
        observed=record.get(page) or {}
        if not record.get(page+'_captured_at') or not observed.get('game_key'):
            continue
        if page=='summary' and record.get('record_source')!='settlement':
            continue
        if page=='rank' and (observed.get('tally_pending') or observed.get('delta') is None):
            continue
        keys.add(observed['game_key'])
    return keys


def _one_edit_or_equal(first, second):
    if len(first)==len(second):
        return sum(a!=b for a,b in zip(first,second))<=1
    if abs(len(first)-len(second))!=1:
        return False
    if len(first)>len(second):
        first,second=second,first
    mismatch=next((i for i,char in enumerate(first) if char!=second[i]),len(first))
    return first[mismatch:]==second[mismatch+1:]


def _hud_footer_variant(first, second):
    left,right=(key.split(':') for key in (first,second))
    return (len(left)==len(right)==4 and left[0]==right[0]
            and sum(a!=b for a,b in zip(left[1:3],right[1:3]))<=1
            and all(_one_edit_or_equal(a,b) for a,b in zip(left[1:],right[1:])))


class CaptureJournal:
    def __init__(self, store):
        self.store=store
        self._hud_key_sightings={}
        with store.lock:
            store.db.executescript('''
                CREATE TABLE IF NOT EXISTS capture_rounds (
                    id TEXT PRIMARY KEY, scope TEXT, started REAL, seen REAL, phase TEXT,
                    match_id TEXT, discarded INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS capture_jobs (
                    id TEXT PRIMARY KEY, round_id TEXT, scope TEXT, created REAL, kind TEXT,
                    path TEXT, status TEXT, manual INTEGER, payload TEXT, signature TEXT, error TEXT);
                CREATE INDEX IF NOT EXISTS capture_job_status ON capture_jobs(status,created);
                CREATE INDEX IF NOT EXISTS capture_round_scope ON capture_rounds(scope,started);
                CREATE TABLE IF NOT EXISTS lobby_history (
                    id TEXT PRIMARY KEY, scope TEXT, created REAL, round_id TEXT, payload TEXT, path TEXT);
                CREATE TABLE IF NOT EXISTS recorder_flags (name TEXT PRIMARY KEY,value INTEGER);
                CREATE TABLE IF NOT EXISTS recognition_retries (key TEXT PRIMARY KEY, attempts INTEGER);
            ''')
            store.db.commit()
            if 'mode' not in [r[1] for r in store.db.execute('PRAGMA table_info(capture_rounds)')]:
                store.db.execute("ALTER TABLE capture_rounds ADD COLUMN mode TEXT DEFAULT 'unknown'")
            if 'game_key' not in [r[1] for r in store.db.execute('PRAGMA table_info(capture_rounds)')]:
                store.db.execute('ALTER TABLE capture_rounds ADD COLUMN game_key TEXT')
            for table in ('capture_rounds','capture_jobs','lobby_history'):
                for rid,scope in store.db.execute(f'SELECT rowid,scope FROM {table}').fetchall():
                    values=json.loads(scope)
                    if len(values)==4:
                        if table=='capture_rounds':store.db.execute('UPDATE capture_rounds SET mode=? WHERE rowid=?',(values[3],rid))
                        store.db.execute(f'UPDATE {table} SET scope=? WHERE rowid=?',(json.dumps(values[:3],ensure_ascii=False),rid))
            store.db.commit()
            store.db.execute("INSERT OR IGNORE INTO recorder_flags VALUES ('snapshot_fallback_since',?)",(int(time.time()),));store.db.commit()

    def was_running(self):
        with self.store.lock:
            row=self.store.db.execute("SELECT value FROM recorder_flags WHERE name='running'").fetchone()
            return bool(row and row[0])

    def set_running(self, value):
        with self.store.lock:
            self.store.db.execute("INSERT OR REPLACE INTO recorder_flags VALUES ('running',?)",(int(bool(value)),))
            self.store.db.commit()

    def scope(self):
        c=self.store.settings()
        return json.dumps([c['season'],c['split'],nickname(c['self_name'])],ensure_ascii=False)

    def note_screen(self, kind, timestamp=None):
        now=time.time() if timestamp is None else timestamp
        with self.store.lock:
            scope=self.scope()
            row=self.store.db.execute("SELECT id,seen,phase,discarded FROM capture_rounds WHERE scope=? AND phase NOT IN ('upload','alias') ORDER BY started DESC LIMIT 1",(scope,)).fetchone()
            if kind=='other':return row[0] if row else None
            if kind in ('lobby','training','profile') and not row:return None
            new=not row or now-row[1]>1800 or (kind=='hud' and (row[2] in ('settlement','lobby','training','profile') or row[3] or now-row[1]>300)) or (kind=='intro' and row[2] not in ('intro',))
            if new:
                ident=uuid.uuid4().hex[:16]
                self.store.db.execute('INSERT INTO capture_rounds (id,scope,started,seen,phase,match_id,discarded,mode) VALUES (?,?,?,?,?,NULL,0,?)',(ident,scope,now,now,kind,self.store.current_mode()))
            else:
                ident=row[0]
                if row[2]==kind and now-row[1]<30:return ident
                self.store.db.execute('UPDATE capture_rounds SET seen=?,phase=? WHERE id=? AND seen<=?',(now,kind,ident,now))
            self.store.db.commit()
            return ident

    def current_round(self):
        with self.store.lock:
            row=self.store.db.execute("SELECT id,phase FROM capture_rounds WHERE scope=? AND phase NOT IN ('upload','alias') ORDER BY started DESC LIMIT 1",(self.scope(),)).fetchone()
            return {'id':row[0],'phase':row[1]} if row else None

    def bind_game_key(self, round_id, game_key, timestamp, phase='hud', job_id=None, allow_alias=True, observation=None):
        """Visible session text ties HUD and result pages together across long transitions."""
        if not round_id or not game_key:return round_id
        with self.store.lock:
            scope=self.scope()
            old=self.store.db.execute('SELECT game_key,phase,match_id,discarded FROM capture_rounds WHERE id=? AND scope=?',(round_id,scope)).fetchone()
            if not old or old[1]=='upload':return round_id
            if old[3] and old[0]==game_key:return round_id
            if old[1]=='alias':
                target=self.canonical_round(round_id)
                if not target:
                    return None
                # A delayed observation of an already-reconciled fragment
                # cannot create another round under an unrelated footer.
                target_key=self.key_for_round(target)
                if game_key not in (old[0],target_key):
                    return None
                found=(target,)
            else:
                found=self.store.db.execute("SELECT id FROM capture_rounds WHERE scope=? AND game_key=? AND discarded=0 AND phase!='alias' ORDER BY started LIMIT 1",(scope,game_key)).fetchone()
            if phase=='hud' and observation is not None:
                if found or game_key==old[0]:
                    self._hud_key_sightings.pop(round_id,None)
                else:
                    # A one-frame OCR error must not create a second round mid-game.
                    if not self._hud_key_matches_settlement_format(scope,game_key) or not self._repeat_hud_key(round_id,game_key,timestamp):
                        return None if old[0] else round_id
            if not found and phase=='settlement' and not old[3] and old[1]!='alias' and self._corroborates_key(
                    round_id,old[0],game_key,timestamp,observation,job_id):
                game_key=old[0]
                found=(round_id,)
            if found:
                ident=found[0]
                if allow_alias and ident!=round_id and not old[0] and not old[2] and not old[3]:
                    self.store.db.execute("UPDATE capture_rounds SET phase='alias',game_key=? WHERE id=?",(game_key,round_id))
                self.store.db.execute('UPDATE capture_rounds SET seen=?,phase=? WHERE id=? AND seen<=?',(timestamp,phase,ident,timestamp))
            elif not old[0]:
                ident=round_id
                self.store.db.execute('UPDATE capture_rounds SET game_key=? WHERE id=?',(game_key,ident))
            else:
                ident=uuid.uuid4().hex[:16]
                self.store.db.execute('INSERT INTO capture_rounds (id,scope,started,seen,phase,mode,game_key) VALUES (?,?,?,?,?,?,?)',
                    (ident,scope,timestamp,timestamp,phase,self.store.current_mode(round_id),game_key))
            if job_id:
                self.store.db.execute("""UPDATE capture_jobs SET round_id=?,
                    payload=json_set(COALESCE(payload,'{}'),'$._origin_round_id',
                        COALESCE(json_extract(payload,'$._origin_round_id'),round_id))
                    WHERE id=? AND round_id IS NOT ?""",(ident,job_id,ident))
            self.store.db.commit();return ident

    def _hud_key_matches_settlement_format(self, scope, key):
        row=self.store.db.execute("""SELECT json_extract(m.payload,'$.summary.game_key')
            FROM capture_rounds r JOIN matches m ON r.match_id=m.id
            WHERE r.scope=? AND r.discarded=0 AND r.phase NOT IN ('alias','upload')
            AND json_extract(m.payload,'$.record_source')='settlement'
            AND json_extract(m.payload,'$.summary.game_key')=r.game_key
            ORDER BY r.seen DESC LIMIT 1""",(scope,)).fetchone()
        if not row:
            return True
        # The footer prefix identifies the game build and is stable between rounds.
        # A later summary can establish a new prefix after an update.
        prefix,_,suffix=key.rpartition(':')
        known_prefix,_,known_suffix=row[0].rpartition(':')
        return prefix==known_prefix and len(suffix)==len(known_suffix)

    def _repeat_hud_key(self, round_id, key, timestamp):
        prior=self._hud_key_sightings.get(round_id)
        if prior and prior[0]==key and 2<=timestamp-prior[1]<=120:
            self._hud_key_sightings.pop(round_id,None)
            return True
        if not prior or prior[0]!=key or timestamp-prior[1]>120:
            self._hud_key_sightings[round_id]=(key,timestamp)
            if len(self._hud_key_sightings)>32:
                self._hud_key_sightings.pop(next(iter(self._hud_key_sightings)))
        return False

    def _corroborates_key(self, round_id, known, read, timestamp, observation, job_id):
        if not known or not observation or known==read or len(known)!=len(read):return False
        if known.rpartition(':')[0]!=read.rpartition(':')[0] or sum(a!=b for a,b in zip(known,read))!=1:return False
        own=self.store.settings()['self_name']
        rows=self.store.db.execute("""SELECT payload FROM capture_jobs
            WHERE round_id=? AND created BETWEEN ? AND ? AND id IS NOT ?
              AND status NOT IN ('dismissed','archived') AND json_extract(payload,'$.game_key')=?
            ORDER BY created DESC LIMIT 8""",(round_id,timestamp-15,timestamp+15,job_id,known))
        return any(_same_settlement(json.loads(payload),observation,own) for payload, in rows)

    def key_for_round(self, round_id):
        with self.store.lock:
            row=self.store.db.execute('SELECT game_key FROM capture_rounds WHERE id=? AND scope=? AND discarded=0',
                                      (round_id,self.scope())).fetchone()
            return row[0] if row else None

    def canonical_round(self, round_id):
        """Resolve a saved alias without following an unrelated game's footer."""
        with self.store.lock:
            scope=self.scope()
            row=self.store.db.execute("""SELECT phase,game_key,match_id FROM capture_rounds
                WHERE id=? AND scope=? AND discarded=0""",(round_id,scope)).fetchone()
            if not row or row[0] in ('training','upload'):
                return None
            phase,key,match_id=row
            if phase!='alias':
                return round_id
            if match_id:
                target=self.store.db.execute("""SELECT r.id FROM capture_rounds r JOIN matches m ON r.match_id=m.id
                    WHERE m.id=? AND r.id=json_extract(m.payload,'$.round_id') AND r.scope=? AND r.discarded=0
                    AND r.phase NOT IN ('alias','upload','training')
                    AND json_extract(m.payload,'$.merged_into') IS NULL
                    AND json_extract(m.payload,'$.deleted_at') IS NULL""",(match_id,scope)).fetchone()
                return target[0] if target else None
            if not key:
                return None
            targets=self.store.db.execute("""SELECT id FROM capture_rounds WHERE scope=? AND game_key=?
                AND discarded=0 AND phase NOT IN ('alias','upload','training') LIMIT 2""",(scope,key)).fetchall()
            return targets[0][0] if len(targets)==1 else None

    def overlapping_hud_fragment(self, snapshot, settlement):
        """Require a plausible OCR error, matching roster and an observed time window."""
        if (snapshot.get('record_source')!='last_snapshot' or snapshot.get('record_origin')
                or snapshot.get('rank') or snapshot.get('round_start_source')=='intro'
                or settlement.get('record_source')!='settlement'
                or any(snapshot.get(k)!=settlement.get(k) for k in ('account','season','split'))):
            return False
        confirmed=settlement_keys(settlement)
        provisional=self.game_keys(snapshot['id'])
        if not confirmed or not provisional or not all(
                any(_hud_footer_variant(weak,strong) for strong in confirmed) for weak in provisional):
            return False
        try:
            at=datetime.fromisoformat(snapshot['snapshot_at']).timestamp()
            start=datetime.fromisoformat(settlement['round_started_at']).timestamp()
            end=datetime.fromisoformat(settlement['summary_captured_at']).timestamp()
        except (KeyError,TypeError,ValueError):
            return False
        if not start<=at<=end or end-at>180:
            return False
        owner=snapshot.get('account')
        def roster(match):
            return {nickname(p.get('name','')) for p in (match.get('summary') or {}).get('players',[])}
        earlier,later=roster(snapshot),roster(settlement)
        if not owner or owner not in earlier or '' in earlier or not earlier<=later:
            return False
        return not self.store.db.execute("""SELECT 1 FROM capture_rounds r LEFT JOIN matches m ON m.id=r.match_id
            WHERE r.scope=? AND r.discarded=0 AND r.phase NOT IN ('alias','upload')
              AND r.started BETWEEN ? AND ? AND COALESCE(r.match_id,'') NOT IN (?,?)
              AND (r.phase='intro' OR json_extract(m.payload,'$.round_start_source')='intro')
            LIMIT 1""",(self.scope(),at,end,snapshot['id'],settlement['id'])).fetchone()

    def _matching_pages(self, match, result, timestamp, scope, round_ids):
        other=OTHER_SETTLEMENT_PAGE[result['kind']]
        page=match.get(other) or {}
        captured=match.get(other+'_captured_at')
        placement=result.get('placement')
        if (not captured or type(placement) is not int or placement<1
                or type(page.get('placement')) is not int or page['placement']!=placement):
            return False
        if result.get('game_key') and page.get('game_key') not in (None,result['game_key']):return False
        observed=datetime.fromisoformat(captured).timestamp()
        earlier,later=sorted((observed,timestamp))
        if later-earlier>SETTLEMENT_PAIR_SECONDS:return False
        return not self.store.db.execute("""SELECT 1 FROM capture_rounds WHERE scope=? AND started>?
            AND started<=? AND id NOT IN (?,?) AND discarded=0 AND phase NOT IN ('alias','upload')
            LIMIT 1""",(scope,earlier,later,*round_ids)).fetchone()

    def _matching_snapshot(self, source, summary):
        if source.get('record_source')!='last_snapshot':return False
        own=nickname(self.store.settings()['self_name'])
        earlier={nickname(p.get('name','')):p for p in (source.get('summary') or {}).get('players',[])}
        final={nickname(p.get('name','')):p for p in summary.get('players',[])}
        if own not in earlier or own not in final or not earlier.keys()<=final.keys():return False
        comparable=[(earlier[own][field],final[own][field]) for field in ('kills','assists','damage')
                    if type(earlier[own].get(field)) is int and type(final[own].get(field)) is int]
        return bool(comparable) and all(before<=after for before,after in comparable)

    def _recent_snapshot_without_new_opening(self, source, target_id, timestamp, scope):
        try:
            captured=datetime.fromisoformat(source['snapshot_at']).timestamp()
        except (KeyError,TypeError,ValueError):
            return False
        return 0<=timestamp-captured<=180 and not self.store.db.execute("""
            SELECT 1 FROM capture_rounds r LEFT JOIN matches m ON m.id=r.match_id
            WHERE r.scope=? AND r.discarded=0 AND r.phase NOT IN ('alias','upload')
              AND r.started>? AND r.started<=? AND r.id NOT IN (?,?)
              AND (r.phase='intro' OR json_extract(m.payload,'$.round_start_source')='intro')
            LIMIT 1""",(scope,captured,timestamp,source['round_id'],target_id)).fetchone()

    @staticmethod
    def _same_session_suffix(first, second):
        if not first or not second:
            return False
        # The trailing hex token identifies this game; the other footer
        # components are shared across many games and may be truncated by OCR.
        a,b=(key.split(':') for key in (first,second))
        return (len(a)==len(b)==4 and a[0]==b[0] and a[-1]==b[-1]
                and len(a[-1])==8 and all(char in '0123456789abcdef' for char in a[-1]))

    def _matching_relinked_hud(self, provisional, canonical, summary, timestamp):
        """Two independently owned, matching HUDs can corroborate a misread footer."""
        owner=nickname(self.store.settings()['self_name'])
        names={nickname(p.get('name','')) for p in summary.get('players',[])}
        if len(names)!=3 or '' in names or owner not in names:
            return False
        sides={provisional:[],canonical:[]}
        rows=self.store.db.execute("""SELECT created,payload FROM hud_backups WHERE created BETWEEN ? AND ?
            AND json_extract(payload,'$.round_id') IN (?,?) AND json_extract(payload,'$.account')=?
            AND json_extract(payload,'$.identity')='self' ORDER BY created DESC""",
            (timestamp-240,timestamp,provisional,canonical,owner)).fetchall()
        for at,raw in rows:
            hud=json.loads(raw)
            roster={nickname(p.get('name','')) for p in hud.get('players',[])}
            if owner in roster and roster<=names:
                sides[hud['round_id']].append((at,hud,roster))
        if not all(sides.values()) or any(not any(roster==names for _,_,roster in side) for side in sides.values()):
            return False
        first,second=sides.values()
        # The two records must contain overlapping observations, not similar
        # statistics from two successive short games with the same squad.
        if (abs(first[0][0]-second[0][0])>20
                or max(first[-1][0],second[-1][0])>min(first[0][0],second[0][0])):
            return False
        def latest_damage(side):
            return next((hud for at,hud,_ in side if at>=timestamp-120
                         and type(hud.get('damage')) is int and hud['damage']>0),None)
        left,right=latest_damage(first),latest_damage(second)
        if not left or not right or left['damage']!=right['damage']:
            return False
        for field in ('kills','assists','participation'):
            if left.get(field) is not None and right.get(field) is not None and left[field]!=right[field]:
                return False
        final=next(p for p in summary['players'] if nickname(p.get('name',''))==owner)
        return type(final.get('damage')) is int and final['damage']>=left['damage']

    def reconcile_settlement(self, provisional, canonical, result, timestamp):
        """Move an already saved complementary page after a verified session relink."""
        if not provisional or not canonical or provisional==canonical or not result.get('game_key'):return False
        with self.store.lock,self.store.db:
            scope=self.scope()
            rounds={rid:(phase,key,mid) for rid,phase,key,mid in self.store.db.execute(
                'SELECT id,phase,game_key,match_id FROM capture_rounds WHERE scope=? AND discarded=0 AND id IN (?,?)',
                (scope,provisional,canonical))}
            old=rounds.get(provisional)
            new=rounds.get(canonical)
            if not old or old[0] not in ('settlement','lobby','profile'):return False
            if not new or new[0] in ('alias','upload') or new[1]!=result['game_key']:return False
            same_suffix=self._same_session_suffix(old[1],new[1])
            same_hud=(not same_suffix and result['kind']=='summary' and type(result.get('placement')) is int
                      and self._matching_relinked_hud(provisional,canonical,result,timestamp))
            if old[1] is not None and old[1]!=new[1] and not (same_suffix or same_hud):return False
            if not old[2]:
                self.store.db.execute("UPDATE capture_rounds SET phase='alias',game_key=?,match_id=? WHERE id=?",
                                      (result['game_key'],new[2],provisional))
                return True
            records={m['id']:m for m in self.store.all() if m['id'] in (old[2],new[2])}
            source=records.get(old[2])
            target=records.get(new[2])
            kind=result['kind']
            other=OTHER_SETTLEMENT_PAGE[kind]
            if (not source or source.get('corrected') or source.get('record_origin')
                    or source.get('round_id')!=provisional
                    or source.get('mode')!='ranked' or (new[2] and not target)):
                return False
            if source.get(other):
                if not self._matching_pages(source,result,timestamp,scope,(provisional,canonical)):return False
            elif not same_hud and not (
                    kind=='summary' and same_suffix and self._matching_snapshot(source,result)
                    and self._recent_snapshot_without_new_opening(source,canonical,timestamp,scope)):
                return False
            if source.get(kind):
                if kind!='summary' or source.get('record_source')!='last_snapshot':return False
                if not (same_hud or same_suffix) and not self._matching_snapshot(source,result):return False
            if target:
                if (target['id']==source['id'] or target.get('corrected') or target.get('record_origin')
                        or target.get(other) or target.get('mode') not in ('ranked','unknown',None)
                        or (target.get(kind) or {}).get('placement') not in (None,result['placement'])):
                    return False
                from matchlog.match_audit import archive_merged_record
                if source.get(other):
                    target[other]=source[other]
                    target[other+'_captured_at']=source[other+'_captured_at']
                for field in ('round_started_at','map_name','snapshot_at','snapshot_backup_id',
                              'snapshot_live_rp','_snapshot_created'):
                    if target.get(field) is None and source.get(field) is not None:
                        target[field]=source[field]
                archive_merged_record(self.store,source,target,'同一场次的总结与分数',preserve_highlights=True)
            else:
                target=source
                target['round_id']=canonical
            target.update(mode='ranked',mode_source='rank_page')
            self.store._save(target,commit=False)
            self.store.db.execute('UPDATE capture_rounds SET match_id=? WHERE id=?',(target['id'],canonical))
            self.store.db.execute("UPDATE capture_rounds SET phase='alias',game_key=?,match_id=? WHERE id=?",
                                  (result['game_key'],target['id'],provisional))
            return True

    def settlement_alias(self, round_id, result, timestamp):
        """Late keyless pages from an alias need its already verified complementary page."""
        with self.store.lock:
            scope=self.scope()
            alias=self.store.db.execute("SELECT game_key FROM capture_rounds WHERE id=? AND scope=? AND phase='alias'",
                                        (round_id,scope)).fetchone()
            if not alias:return round_id
            canonical=self.canonical_round(round_id)
            row=self.store.db.execute("""SELECT m.payload FROM capture_rounds r JOIN matches m ON m.id=r.match_id
                WHERE r.id=? AND r.scope=? AND r.discarded=0""",(canonical,scope)).fetchone()
            if not row:return None
            match=json.loads(row[0])
            if match.get('deleted_at') or match.get('merged_into'):return None
            if not self._matching_pages(match,result,timestamp,scope,(round_id,canonical)):return None
            same_page=match.get(result['kind'])
            if same_page and any(same_page.get(key)!=value for key,value in result.items() if key not in ('tokens','size')):
                return None
            return canonical

    def upload_round(self, match_id=None, game_key=None):
        with self.store.lock:
            match=next((m for m in self.store.all() if m['id']==match_id),None) if match_id else None
            cfg=self.store.settings()
            if match_id and not match:raise ValueError('对局不存在')
            if match and (match['season'],match['split'])!=(cfg['season'],cfg['split']):
                raise ValueError('请切换到该对局的赛季、赛段后补全')
            if match and match.get('round_id'):return match['round_id'],match['created']
            ident=uuid.uuid4().hex[:16];now=time.time()
            self.store.db.execute('INSERT INTO capture_rounds (id,scope,started,seen,phase,match_id,discarded,mode,game_key) VALUES (?,?,?,?,?,?,0,?,?)',
                (ident,self.scope(),now,now,'upload',match_id,match.get('mode','unknown') if match else 'unknown',game_key))
            if match:
                match['round_id']=ident;self.store._save(match,commit=False)
            self.store.db.commit()
            return ident,match['created'] if match else now

    def match_for_game_key(self, game_key):
        with self.store.lock:
            ids=self.store.db.execute("""SELECT DISTINCT match_id FROM capture_rounds WHERE scope=? AND game_key=?
                    AND discarded=0 AND match_id IS NOT NULL""",(self.scope(),game_key)).fetchall()
            if len(ids)!=1:return None
            return ids[0][0]

    def game_keys(self, match_id):
        with self.store.lock:
            return {key for key, in self.store.db.execute("""SELECT game_key FROM capture_rounds
                WHERE scope=? AND match_id=? AND discarded=0 AND game_key IS NOT NULL""",(self.scope(),match_id))}

    def enqueue(self, image, kind, round_id=None, manual=False, timestamp=None, *,
                hud_key=None, hud_roster=True, client_px=None, managed_hud=False, hud_tail=False):
        now=time.time() if timestamp is None else timestamp
        image=queued_frame(image,kind)
        with self.store.lock, self.store.db:
            scope=self.scope()
            if round_id:
                row=self.store.db.execute('SELECT discarded FROM capture_rounds WHERE id=? AND scope=?',(round_id,scope)).fetchone()
                if not row or row[0]:return None
            if kind in ('lobby','probe','profile') and not manual:
                if self.store.db.execute("SELECT 1 FROM capture_jobs WHERE scope=? AND kind=? AND round_id IS ? AND status IN ('pending','processing')",(scope,kind,round_id)).fetchone():return None
            ident=uuid.uuid4().hex[:20]
            from matchlog.image_storage import CHINA, cached_asset, capture_name, remember_asset, save_archive, timestamp_path
            managed_hud=bool(managed_hud and kind=='hud' and round_id and not manual)
            if managed_hud:
                if not self.trim_hud(round_id,reserve=1):return None
                folder=media_dir(self.store.root,'hud')
                stamp=datetime.fromtimestamp(now,CHINA)
                serial=1
                while True:
                    path=folder/capture_name('hud-frame',stamp,'.webp',serial)
                    if not path.exists() and not self.store.db.execute(
                        'SELECT 1 FROM capture_jobs WHERE path=? LIMIT 1',(str(path),),
                    ).fetchone():break
                    serial+=1
                save_archive(image,path)
            else:
                asset=frame_asset(image)
                folder=media_dir(self.store.root,'inbox')
                path=cached_asset(self.store,'queue',asset,folder)
                if path is None:
                    folder.mkdir(parents=True,exist_ok=True)
                    path=timestamp_path(folder,'queue',now,'.png')
                    temp=path.with_suffix('.part')
                    image.save(temp,format='PNG',compress_level=2)
                    temp.replace(path)
                    remember_asset(self.store,'queue',asset,path)
            deferred=kind=='hud' and not manual
            payload={'_client_px':list(client_px)} if client_px else {}
            if deferred:
                payload['_hud_roster']=bool(hud_roster)
                if managed_hud:payload['_hud_managed']=True
                if hud_tail:payload['_hud_tail']=True
                if hud_key is not None:
                    payload.update(_hud_capture=True,_hud_key=hud_key.hex() if isinstance(hud_key,bytes) else str(hud_key))
            self.store.db.execute('INSERT INTO capture_jobs VALUES (?,?,?,?,?,?,?,?,?,NULL,NULL)',
                (ident,round_id,scope,now,kind,str(path.resolve()),'processing' if hud_tail else 'deferred' if deferred else 'pending',int(manual),
                 json.dumps(payload) if payload else None))
            if deferred and round_id and not managed_hud:
                self._bound_hud(scope,round_id)
            if kind=='settlement' and not manual and round_id:
                # Keep the newest frame and its newest independent confirmation.
                self.store.db.execute("""WITH duplicates AS (
                    SELECT id, created, ROW_NUMBER() OVER (ORDER BY created DESC,rowid DESC) AS position,
                           MAX(created) OVER () AS latest
                    FROM capture_jobs
                    WHERE scope=? AND round_id=? AND kind='settlement' AND status='pending'
                      AND manual=0 AND path=?
                ), confirmation AS (
                    SELECT COALESCE(MIN(CASE WHEN latest-created>=.3 THEN position END),MAX(position)) AS position
                    FROM duplicates
                ) UPDATE capture_jobs SET status='archived',error='同帧重复采集，原图保留'
                WHERE id IN (SELECT id FROM duplicates WHERE position NOT IN (1,(SELECT position FROM confirmation)))""",
                    (scope,round_id,str(path.resolve())))
            # Keep recent settlement and explicit captures ahead of background probes.
            self.store.db.execute("""UPDATE capture_jobs SET status='review',
                error='后台繁忙，原图已保留，可手动补录' WHERE id IN (
                    SELECT id FROM capture_jobs WHERE scope=? AND status='pending'
                    ORDER BY manual DESC, (kind='settlement') DESC, created DESC LIMIT -1 OFFSET 64
                )""", (scope,))
            return ident

    def trim_hud(self, round_id, *, reserve=0):
        """Rotate automatic evidence files, including the ones already recognized."""
        if not round_id:return False
        with self.store.lock,self.store.db:
            db=self.store.db
            scope=self.scope()
            row=db.execute('SELECT match_id,game_key FROM capture_rounds WHERE id=? AND scope=?',(round_id,scope)).fetchone()
            if row is None:return False
            rows=db.execute("""SELECT j.id,j.path,j.status,j.payload FROM capture_jobs j
                JOIN capture_rounds r ON j.round_id=r.id AND j.scope=r.scope
                WHERE j.scope=? AND (r.id=? OR (? IS NOT NULL AND r.match_id=?)
                  OR (? IS NOT NULL AND r.phase='alias' AND r.game_key=?))
                  AND j.kind='hud' AND j.manual=0 AND json_extract(j.payload,'$._hud_managed')=1
                ORDER BY j.created DESC,j.rowid DESC""",(scope,round_id,row[0],row[0],row[1],row[1])).fetchall()
            active={}
            for ident,value,status,raw in rows:
                if path:=existing_media_path(self.store.root,value):
                    active.setdefault(path,(ident,status,json.loads(raw)))
            budget=HUD_FILE_LIMIT-reserve
            if len(active)<=budget:return True
            opening=next((path for path,(_,_,data) in reversed(active.items()) if data.get('_hud_roster')),None)
            priority={path:index for index,path in enumerate(active)}
            for path,(ident,status,_data) in sorted(active.items(),key=lambda item:(
                item[0]==opening,item[1][2].get('_hud_tail',False),
                item[1][1]=='done',item[1][2].get('_hud_roster',False),
                -priority[item[0]],
            )):
                if status=='processing' or path==opening:continue
                if db.execute("""SELECT 1 FROM evidence WHERE path=?
                    UNION ALL SELECT 1 FROM match_highlights WHERE path=?
                    UNION ALL SELECT 1 FROM capture_jobs WHERE path=? AND id!=? LIMIT 1""",
                    (str(path),str(path),str(path),ident)).fetchone():continue
                try:path.unlink(missing_ok=True)
                except OSError:continue
                db.execute("UPDATE capture_jobs SET status='archived',error='已轮换局内原图' WHERE id=?",(ident,))
                budget+=1
                if len(active)<=budget:return True
            return False

    def _bound_hud(self, scope, round_id):
        rows=self.store.db.execute("""SELECT id,COALESCE(json_extract(payload,'$._hud_roster'),1)
            FROM capture_jobs WHERE scope=? AND round_id=? AND kind='hud' AND manual=0 AND status='deferred'
            ORDER BY created DESC,rowid DESC""",(scope,round_id)).fetchall()
        if len(rows)<=HUD_DRAFT_LIMIT:return
        keep={ident for ident,_ in rows[:HUD_DRAFT_LIMIT]}
        if roster:=next((ident for ident,full in rows if full),None):
            if roster not in keep:
                keep.remove(rows[HUD_DRAFT_LIMIT-1][0]);keep.add(roster)
        self.store.db.executemany("UPDATE capture_jobs SET status='archived',error='已保留本局更新的局内快照' WHERE id=?",
                                  [(ident,) for ident,_ in rows if ident not in keep])

    def recover_interrupted(self):
        # Called only by the single desktop instance, not by read-only Api instances.
        with self.store.lock:
            self.store.db.execute("UPDATE capture_jobs SET status='pending' WHERE status='processing'")
            self.store.db.commit()
            folder=media_dir(self.store.root,'hud')
            # A crash between writing the frame and inserting its queue row can
            # leave a generated orphan. Only remove our own unreferenced files.
            for path in folder.glob('hud-frame-*.webp'):
                if not re.fullmatch(r'hud-frame-\d{6}-\d{6}(?:-\d+)?\.webp',path.name):continue
                if not existing_media_path(self.store.root,path):continue
                value=str(path)
                if self.store.db.execute("""SELECT 1 FROM capture_jobs WHERE path=?
                    UNION ALL SELECT 1 FROM hud_backups WHERE path=? OR json_extract(payload,'$.squad_path')=?
                    UNION ALL SELECT 1 FROM evidence WHERE path=?
                    UNION ALL SELECT 1 FROM match_highlights WHERE path=? LIMIT 1""",
                    (value,value,value,value,value)).fetchone():continue
                try:path.unlink(missing_ok=True)
                except OSError:continue

    def requeue_detector_errors(self, now=None):
        """Give frames affected by the 0.11 OCR mode bug one new processing pass."""
        from matchlog.capture_cleanup import DAY
        now=time.time() if now is None else now
        with self.store.lock,self.store.db:
            cutoff=now-self.store.settings()['cleanup_review_days']*DAY
            rows=self.store.db.execute("""SELECT id,path,round_id FROM capture_jobs
                WHERE scope=? AND created>=? AND status='failed'
                  AND kind IN ('settlement','manual','probe','profile')
                  AND error LIKE '%TextRecOutput%has no attribute%boxes%'
                  AND COALESCE((SELECT discarded FROM capture_rounds WHERE id=round_id),0)=0
                """,(self.scope(),cutoff)).fetchall()
            for ident,path,round_id in rows:
                if existing_media_path(self.store.root,path):
                    self.store.db.execute("UPDATE capture_jobs SET status='pending',error=NULL WHERE id=?",(ident,))
                    self.store.db.execute('DELETE FROM recognition_retries WHERE key=?',(_review_retry_key(path,round_id),))

    def get(self, ident):
        with self.store.lock:
            row=self.store.db.execute('SELECT * FROM capture_jobs WHERE id=?',(str(ident),)).fetchone()
            if not row:raise ValueError('采集线索不存在')
            keys=['id','round_id','scope','created','kind','path','status','manual','payload','signature','error']
            result=dict(zip(keys,row))
            result['payload']=json.loads(result['payload']) if result['payload'] else None
            return result

    def claim(self, settlement_only=False, recent_after=None):
        with self.store.lock, self.store.db:
            scope=self.scope()
            cutoff=float('-inf') if recent_after is None else recent_after
            if settlement_only:
                row=self.store.db.execute("""SELECT id FROM capture_jobs
                    WHERE scope=? AND (kind='settlement' OR manual=1) AND status='pending' AND created>=?
                    ORDER BY manual DESC,created DESC,rowid DESC LIMIT 1""", (scope,cutoff)).fetchone()
            else:
                row=self._next_background_job(scope,cutoff)
            if not row:return None
            self.store.db.execute("UPDATE capture_jobs SET status='processing' WHERE id=?",row)
            return self.get(row[0])

    def claim_hud(self, round_id, *, limit=HUD_READ_LIMIT):
        """Read a bounded set of drafts from this exact provisional round."""
        if not round_id:return None
        with self.store.lock,self.store.db:
            scope=self.scope()
            row=self.store.db.execute("""SELECT j.id FROM capture_jobs j JOIN capture_rounds r ON r.id=j.round_id
                WHERE j.scope=? AND j.round_id=? AND j.kind='hud' AND j.manual=0 AND r.discarded=0
                  AND j.status IN ('deferred','pending')
                  AND (SELECT count(*) FROM capture_jobs tried WHERE tried.scope=j.scope AND tried.round_id=j.round_id
                       AND tried.kind='hud' AND tried.manual=0 AND tried.status IN ('done','review','failed','processing'))<?
                ORDER BY (EXISTS (SELECT 1 FROM capture_jobs prior WHERE prior.round_id=j.round_id
                    AND prior.kind='hud' AND prior.status IN ('done','review','failed')
                    AND COALESCE(json_extract(prior.payload,'$._hud_roster'),1)=0)
                    AND COALESCE(json_extract(j.payload,'$._hud_roster'),1)) DESC,
                    j.created DESC,j.rowid DESC LIMIT 1""",(scope,round_id,limit)).fetchone()
            if not row:return None
            self.store.db.execute("UPDATE capture_jobs SET status='processing' WHERE id=?",row)
            return self.get(row[0])

    def claim_hud_roster(self, round_id=None):
        """Reserve two full-team observations independently of score-only HUDs."""
        with self.store.lock,self.store.db:
            row=self.store.db.execute("""SELECT j.id FROM capture_jobs j
                JOIN capture_rounds r ON r.id=j.round_id AND r.scope=j.scope
                WHERE j.scope=? AND (? IS NULL OR j.round_id=?) AND j.kind='hud'
                  AND r.discarded=0 AND j.status IN ('pending','deferred')
                  AND json_extract(j.payload,'$._hud_managed')=1
                  AND json_extract(j.payload,'$._hud_roster')=1
                  AND (SELECT COUNT(*) FROM capture_jobs older WHERE older.round_id=j.round_id
                       AND older.kind='hud' AND json_extract(older.payload,'$._hud_roster')=1
                       AND older.status IN ('processing','done','review','failed'))<2
                ORDER BY j.created,j.rowid LIMIT 1""",(self.scope(),round_id,round_id)).fetchone()
            if row is None:return None
            self.store.db.execute("UPDATE capture_jobs SET status='processing' WHERE id=?",row)
            return self.get(row[0])

    def claim_hud_tail(self):
        """Resume a verified final frame interrupted between writing and committing."""
        with self.store.lock,self.store.db:
            row=self.store.db.execute("""SELECT j.id FROM capture_jobs j
                JOIN capture_rounds r ON r.id=j.round_id AND r.scope=j.scope
                WHERE j.scope=? AND r.discarded=0 AND j.kind='hud'
                  AND j.status IN ('pending','deferred') AND json_extract(j.payload,'$._hud_tail')=1
                ORDER BY j.created DESC,j.rowid DESC LIMIT 1""",(self.scope(),)).fetchone()
            if row is None:return None
            self.store.db.execute("UPDATE capture_jobs SET status='processing' WHERE id=?",row)
            return self.get(row[0])

    def _next_background_job(self, scope, cutoff):
        manual=self.store.db.execute("""SELECT id FROM capture_jobs WHERE scope=? AND status='pending'
            AND manual=1 AND created>=? ORDER BY created DESC,rowid DESC LIMIT 1""",(scope,cutoff)).fetchone()
        if manual:return manual
        # Before settlement OCR, read the latest HUD once to recover the same-round hero roster.
        row=self.store.db.execute("""SELECT h.id FROM capture_jobs h JOIN capture_rounds r ON r.id=h.round_id
            WHERE h.scope=? AND h.kind='hud' AND h.status IN ('pending','deferred') AND r.discarded=0
            AND h.created>=?
            AND (EXISTS (SELECT 1 FROM capture_jobs s WHERE s.round_id=h.round_id AND s.kind IN ('settlement','manual') AND s.status='pending')
                OR r.phase IN ('lobby','settlement','profile','training')
                OR EXISTS (SELECT 1 FROM capture_rounds new WHERE new.scope=h.scope AND new.started>r.started AND new.phase NOT IN ('alias','upload')))
            AND NOT EXISTS (SELECT 1 FROM capture_jobs d WHERE d.round_id=h.round_id AND d.kind='hud' AND d.status='done')
            AND (SELECT count(*) FROM capture_jobs t WHERE t.round_id=h.round_id AND t.kind='hud'
                 AND t.status IN ('review','failed','processing'))<?
            ORDER BY h.created DESC,h.rowid DESC LIMIT 1""",(scope,cutoff,HUD_READ_LIMIT)).fetchone()
        if row:return row
        row=self.store.db.execute("""SELECT id FROM capture_jobs WHERE scope=? AND status='pending'
            AND kind!='hud' AND created>=? ORDER BY CASE kind WHEN 'settlement' THEN 0 WHEN 'lobby' THEN 1 ELSE 2 END,
            created LIMIT 1""",(scope,cutoff)).fetchone()
        if row:return row
        # A second frame can supply the missing identity or combat fields after the newest one.
        return self.store.db.execute("""SELECT h.id FROM capture_jobs h JOIN capture_rounds r ON r.id=h.round_id
            WHERE h.scope=? AND h.kind='hud' AND h.status IN ('pending','deferred') AND r.discarded=0
              AND h.created>=? AND r.phase NOT IN ('alias','upload')
              AND (r.phase IN ('lobby','settlement','profile','training')
                OR EXISTS (SELECT 1 FROM capture_rounds new WHERE new.scope=h.scope AND new.started>r.started AND new.phase NOT IN ('alias','upload')))
              AND (SELECT count(*) FROM capture_jobs t WHERE t.round_id=h.round_id AND t.kind='hud'
                   AND t.status IN ('done','review','failed','processing'))<?
            ORDER BY h.created DESC,h.rowid DESC LIMIT 1""",(scope,cutoff,HUD_READ_LIMIT)).fetchone()

    def archive_earlier_hud(self, round_id, captured_at):
        with self.store.lock:
            scope=self.scope()
            latest=self.store.db.execute("""SELECT COALESCE(json_extract(payload,'$._hud_roster'),1)
                FROM capture_jobs WHERE scope=? AND round_id=? AND kind='hud' AND created=?
                ORDER BY rowid DESC LIMIT 1""",(scope,round_id,captured_at)).fetchone()
            keep=None
            if latest and not latest[0]:
                row=self.store.db.execute("""SELECT id FROM capture_jobs WHERE scope=? AND round_id=? AND kind='hud'
                    AND status='deferred' AND created<? AND COALESCE(json_extract(payload,'$._hud_roster'),1)=1
                    ORDER BY created DESC,rowid DESC LIMIT 1""",(scope,round_id,captured_at)).fetchone()
                keep=row[0] if row else None
            self.store.db.execute("""UPDATE capture_jobs SET status='archived',error='已用本局更新快照，原图保留'
                WHERE scope=? AND round_id=? AND kind='hud' AND status='deferred' AND created<? AND id IS NOT ?""",
                (scope,round_id,captured_at,keep))
            self.store.db.commit()

    def observe(self, ident, payload, signature):
        with self.store.lock:
            job=self.get(ident)
            metadata={key:value for key,value in (job['payload'] or {}).items()
                      if key in ('_origin_round_id','_client_px')}
            payload={**payload,**metadata}
            self.store.db.execute('UPDATE capture_jobs SET payload=?,signature=? WHERE id=?',
                (json.dumps(payload,ensure_ascii=False),signature,ident));self.store.db.commit()
            times=[r[0] for r in self.store.db.execute("SELECT created FROM capture_jobs WHERE round_id=? AND signature=? AND status!='dismissed'",(job['round_id'],signature))]
            return len(times)>=2 and max(times)-min(times)>=.3

    def finish(self, ident, status, error=None):
        with self.store.lock:
            self.store.db.execute('UPDATE capture_jobs SET status=?,error=? WHERE id=? AND status!=?',(status,error,ident,'dismissed'))
            self.store.db.commit()
            if status=='done':
                path=self.get(ident)['path']
                referenced=self.store.db.execute("""SELECT 1 FROM capture_jobs WHERE path=? AND status NOT IN ('done','dismissed')
                    UNION ALL SELECT 1 FROM hud_backups WHERE path=? OR json_extract(payload,'$.squad_path')=? LIMIT 1""",
                    (path,path,path)).fetchone()
                if not referenced and (safe:=existing_media_path(self.store.root,path)):safe.unlink(missing_ok=True)

    def resolve_page(self, round_id, kind):
        with self.store.lock:
            rows=self.store.db.execute("SELECT id,payload FROM capture_jobs WHERE round_id=? AND status IN ('review','failed')",(round_id,)).fetchall()
            for ident,payload in rows:
                if payload and json.loads(payload).get('kind')==kind:self.finish(ident,'done')

    def recovery_candidates(self):
        """Limit the retry batch after excluding exhausted and irrelevant jobs."""
        with self.store.lock:
            rows=self.store.db.execute("""SELECT j.id,j.path,j.round_id
                FROM capture_jobs j
                LEFT JOIN capture_rounds r ON r.id=j.round_id
                WHERE j.scope=? AND j.status IN ('review','failed')
                  AND COALESCE(r.discarded,0)=0
                  AND (j.kind IN ('settlement','manual','probe','lobby')
                    OR (j.kind='hud' AND r.match_id IS NOT NULL))
                ORDER BY (j.kind IN ('lobby','hud')),j.created DESC""",
                (self.scope(),)).fetchall()
            retries=dict(self.store.db.execute("SELECT key,attempts FROM recognition_retries WHERE key GLOB 'v2:job:*'"))
        candidates=[]
        for ident,path,rid in rows:
            key=_review_retry_key(path,rid)
            attempts=retries.get(key,0)
            if attempts<2:
                candidates.append((ident,key,attempts))
                if len(candidates)==30:break
        return candidates

    def public(self):
        with self.store.lock:
            ids=self.store.db.execute("SELECT id FROM capture_jobs WHERE scope=? AND status IN ('review','failed','pending','processing','deferred') ORDER BY created DESC LIMIT 30",(self.scope(),)).fetchall()
            return [{k:v for k,v in self.get(row[0]).items() if k not in ('path','scope','signature')} for row in ids]

    def save_lobby(self, payload, path, round_id=None, timestamp=None):
        now=time.time() if timestamp is None else timestamp
        with self.store.lock:
            self.store.db.execute('INSERT INTO lobby_history VALUES (?,?,?,?,?,?)',
                (uuid.uuid4().hex[:16],self.scope(),now,round_id,json.dumps(payload,ensure_ascii=False),str(Path(path).resolve())))
            self.store.db.commit()

    def lobby_samples(self):
        with self.store.lock:
            rows=self.store.db.execute('SELECT created,payload FROM lobby_history WHERE scope=? ORDER BY created DESC LIMIT 10',(self.scope(),)).fetchall()
            return [{'created':created,**{k:v for k,v in json.loads(payload).items() if k not in ('tokens','size')}} for created,payload in rows]
