from datetime import datetime, timezone, timedelta
from pathlib import Path
import hashlib
import json
import sqlite3
import threading
import time
import uuid
from matchlog.recognition import nickname,display_name
from matchlog.heroes import canonical_hero
from matchlog.config import DEFAULTS, HIGHLIGHT_LIMITS, MAX_RETENTION_DAYS, RETENTION_LIMITS, SETTLEMENT_TONES, THEMES
from matchlog.media_paths import media_dir


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True,exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root/'matches.sqlite3',check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS matches (id TEXT PRIMARY KEY, created REAL NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS evidence (hash TEXT PRIMARY KEY, match_id TEXT, kind TEXT, path TEXT);
            CREATE TABLE IF NOT EXISTS hud_backups (id TEXT PRIMARY KEY, created REAL, season TEXT, split TEXT, payload TEXT, path TEXT, match_id TEXT);
            CREATE TABLE IF NOT EXISTS lobby_rank (scope TEXT PRIMARY KEY, created REAL, payload TEXT, path TEXT);
            CREATE TABLE IF NOT EXISTS rp_checks (id TEXT PRIMARY KEY, scope TEXT, created REAL, payload TEXT);
            CREATE TABLE IF NOT EXISTS profile_snapshots (id TEXT PRIMARY KEY, scope TEXT, created REAL, payload TEXT, path TEXT);
            CREATE TABLE IF NOT EXISTS match_highlights (match_id TEXT PRIMARY KEY,path TEXT,hash TEXT,created REAL,payload TEXT);
            CREATE TABLE IF NOT EXISTS capture_trace (job_id TEXT PRIMARY KEY,round_id TEXT,origin_round_id TEXT,finished REAL,payload TEXT);
            CREATE TABLE IF NOT EXISTS media_asset_paths (category TEXT,digest TEXT,path TEXT,PRIMARY KEY(category,digest));''')
        self.db.commit()
        self.active_id=None
        self.phase=None
        self.hud=None
        self.last_ingest=0
        from matchlog.capture_journal import CaptureJournal
        self.journal=CaptureJournal(self)
        from matchlog.migrations import migrate
        migrate(self)
        self._init_view_revision()

    def _init_view_revision(self):
        """Separate dashboard changes from capture queue bookkeeping."""
        self.db.execute('CREATE TEMP TABLE dashboard_revision (value INTEGER NOT NULL)')
        self.db.execute('INSERT INTO dashboard_revision VALUES (0)')
        for table in ('matches','settings','lobby_rank','rp_checks','profile_snapshots','match_highlights'):
            for operation in ('INSERT','UPDATE','DELETE'):
                self.db.execute(f'''CREATE TEMP TRIGGER dashboard_{table}_{operation}
                    AFTER {operation} ON main.{table} BEGIN
                    UPDATE dashboard_revision SET value=value+1; END''')
        self.db.commit()

    def dashboard_revision(self):
        return self.db.execute('SELECT value FROM dashboard_revision').fetchone()[0]

    def settings(self):
        with self.lock:
            row=self.db.execute('SELECT payload FROM settings WHERE id=1').fetchone()
            stored=json.loads(row[0]) if row else {}
            config={**DEFAULTS,**stored}
            for event,old in [('end','detect'),('summary','success'),('rank','success')]:
                key=f'sound_{event}_enabled'
                if key not in stored and f'notify_{old}_enabled' in stored:
                    config[key]=bool(stored[f'notify_{old}_enabled'])
            if config['settlement_tone'] not in SETTLEMENT_TONES:
                config['settlement_tone']=DEFAULTS['settlement_tone']
            config.pop('clan',None)
            for key in ('query_id','query_mode','baseline','baseline_at'):config.pop(key,None)
            return config

    def configure(self, patch):
        with self.lock:
            config=self.settings()
            previous=config.copy()
            for key in ['self_name','season','split']:
                if key in patch:
                    value=str(patch[key]).strip()[:64]
                    if not value or (key=='self_name' and not nickname(value)):
                        raise ValueError('昵称、赛季和赛段不能为空')
                    config[key]=value
            if 'theme' in patch:
                if patch['theme'] not in THEMES:raise ValueError('主题不正确')
                config['theme']=patch['theme']
            if 'settlement_tone' in patch:
                if patch['settlement_tone'] not in SETTLEMENT_TONES:raise ValueError('结算音效不正确')
                config['settlement_tone']=patch['settlement_tone']
            if 'auto_cleanup' in patch:config['auto_cleanup']=bool(patch['auto_cleanup'])
            for key, minimum in RETENTION_LIMITS.items():
                if key in patch:
                    raw=str(patch[key]).strip()
                    if not raw.isascii() or not raw.isdecimal() or len(raw)>4 or not minimum<=int(raw)<=MAX_RETENTION_DAYS:
                        raise ValueError(f'保留天数应为 {minimum}–{MAX_RETENTION_DAYS} 的整数')
                    config[key]=int(raw)
            for key, maximum in HIGHLIGHT_LIMITS.items():
                if key in patch:
                    raw=str(patch[key]).strip()
                    if not raw.isascii() or not raw.isdecimal() or len(raw)>6 or int(raw)>maximum:
                        raise ValueError(f'高光门槛应为 0–{maximum} 的整数')
                    config[key]=int(raw)
            if 'game_mode' in patch:
                if patch['game_mode'] not in ('ranked','casual','event'):raise ValueError('对局模式不正确')
                if config['game_mode']!=patch['game_mode']:self.active_id=None
                config['game_mode']=patch['game_mode']
                config['detected_mode']=patch['game_mode']
            if 'friends' in patch:
                config['friends']=list(dict.fromkeys(str(x).strip()[:64] for x in patch['friends'] if str(x).strip()))[:20]
            if 'auto_switch' in patch:
                config['auto_switch']=bool(patch['auto_switch'])
            for key in (
                'auto_season','start_on_login','debug_mode','low_impact','reduce_motion','show_hero_board',
                'sound_end_enabled','sound_summary_enabled','sound_rank_enabled',
                'notify_detect_enabled','notify_success_enabled','notify_pending_enabled',
            ):
                if key in patch:config[key]=bool(patch[key])
            for key in ['notify_detect_seconds','notify_success_seconds','notify_pending_seconds']:
                if key in patch:
                    value=float(patch[key])
                    if not 1<=value<=60:raise ValueError('提示时长应为 1–60 秒')
                    config[key]=value
            if 'initial_rp' in patch:
                value=patch['initial_rp']
                value=None if value in (None,'') else int(value)
                if value is not None and not 0<=value<=2000000:raise ValueError('起始分数超出范围')
                if value!=config.get('initial_rp'):
                    config['initial_rp_at']=datetime.now(timezone(timedelta(hours=8))).isoformat() if value is not None else None
                config['initial_rp']=value
            account_changed=nickname(config['self_name'])!=nickname(previous['self_name'])
            if account_changed or (config['season'],config['split'])!=(previous['season'],previous['split']):
                self.active_id=None
                self.phase=None
                self.hud=None
                config['initial_rp']=None;config['initial_rp_at']=None
            if account_changed:
                config.update(detected_mode='unknown',detected_mode_at=0)
            self.db.execute('INSERT OR REPLACE INTO settings VALUES (1,?)',(json.dumps(config,ensure_ascii=False),))
            self.db.commit()
            return config

    def _save(self, match, commit=True):
        self.db.execute('INSERT OR REPLACE INTO matches VALUES (?,?,?)',
                        (match['id'],match['created'],json.dumps(match,ensure_ascii=False)))
        if commit:self.db.commit()

    def restore_settings(self, config):
        with self.lock,self.db:
            self.db.execute('INSERT OR REPLACE INTO settings VALUES (1,?)',(json.dumps(config,ensure_ascii=False),))
            self.active_id=None
            self.phase=None
            self.hud=None

    def all(self):
        with self.lock:
            account=nickname(self.settings()['self_name'])
            if not account:return []
            matches=[m for (raw,) in self.db.execute(
                "SELECT payload FROM matches WHERE json_extract(payload,'$.account')=? ORDER BY created DESC",
                (account,)) if not (m:=json.loads(raw)).get('merged_into') and not m.get('deleted_at')]
            uploads={r[0]:r[1] for r in self.db.execute("SELECT id,started FROM capture_rounds WHERE phase='upload'")}
            for m in matches:
                for p in (m.get('summary') or {}).get('players',[]):
                    if p.get('hero'):p['hero']=canonical_hero(p['hero'])
                if m.get('round_id') in uploads and abs(m['created']-uploads[m['round_id']])<2 and not m.get('time_corrected'):
                    m['record_origin']='upload'
            return matches

    def profile_isolation(self):
        with self.lock:
            count=self.db.execute("""SELECT COUNT(*) FROM matches
                WHERE json_extract(payload,'$.account') IS NULL
                AND json_extract(payload,'$.merged_into') IS NULL
                AND json_extract(payload,'$.deleted_at') IS NULL""").fetchone()[0]
            return {'unassigned':count}

    def match_count_all_accounts(self):
        with self.lock:
            return self.db.execute("""SELECT COUNT(*) FROM matches
                WHERE json_extract(payload,'$.merged_into') IS NULL
                AND json_extract(payload,'$.deleted_at') IS NULL""").fetchone()[0]

    def current_mode(self,round_id=None):
        with self.lock:
            if round_id:
                row=self.db.execute('SELECT mode FROM capture_rounds WHERE id=?',(round_id,)).fetchone()
                if row:return row[0] or 'unknown'
            return self.settings().get('detected_mode','unknown')

    def detect_mode(self,mode,round_id=None,source='screen',timestamp=None):
        if mode not in ('ranked','casual','event'):return
        with self.lock:
            if round_id and not self.db.execute('SELECT 1 FROM capture_rounds WHERE id=? AND scope=?',
                                                (round_id,self.journal.scope())).fetchone():return
            cfg=self.settings()
            at=time.time() if timestamp is None else timestamp
            if at>=cfg.get('detected_mode_at',0):
                cfg.update(detected_mode=mode,game_mode=mode,detected_mode_at=at)
                self.db.execute('INSERT OR REPLACE INTO settings VALUES (1,?)',(json.dumps(cfg,ensure_ascii=False),))
            if round_id and source!='lobby':
                self.db.execute('UPDATE capture_rounds SET mode=? WHERE id=?',(mode,round_id))
                row=self.db.execute('SELECT match_id FROM capture_rounds WHERE id=?',(round_id,)).fetchone()
                match=next((m for m in self.all() if row and m['id']==row[0]),None)
                if match:match.update(mode=mode,mode_source=source);self._save(match,commit=False)
            self.db.commit()

    def attach_heroes(self,match,samples=None,*,add_roster=False):
        from matchlog.heroes import HeroSamples
        changed=False;roster={};rid=match.get('round_id')
        if not rid:return False
        owner=match.get('account') or nickname(self.settings()['self_name'])
        scope=json.dumps([match['season'],match['split'],owner],ensure_ascii=False)
        verified_rosters=[]
        candidates={}
        slot_frames=[]
        settled_names={nickname(p.get('name','')) for p in (match.get('summary') or {}).get('players',[])}
        for raw,game_key,created,path in self.db.execute("""SELECT h.payload,r.game_key,h.created,h.path FROM hud_backups h
                JOIN capture_rounds r ON r.id=json_extract(h.payload,'$.round_id')
                WHERE h.season=? AND h.split=? AND r.scope=? AND r.discarded=0
                AND (r.id=? OR r.match_id=?) ORDER BY h.created DESC""",
                (match['season'],match['split'],scope,rid,match['id'])):
            snapshot=json.loads(raw)
            if (snapshot.get('identity') not in (None,'self') or snapshot.get('account') not in (None,owner)
                    or game_key and snapshot.get('game_key') not in (None,game_key)):continue
            players=snapshot.get('players') or []
            keys=[nickname(p.get('name','')) for p in players]
            owned_roster=(snapshot.get('identity')=='self' and owner in keys and all(keys)
                          and len(set(keys))==len(keys))
            if add_roster and owned_roster:
                verified_rosters.append(players)
            slots={}
            for p in players:
                key=nickname(p.get('name',''))
                if not key:continue
                sample=p.get('hero_sample')
                if sample and samples is None:samples=HeroSamples(media_dir(self.root,'hero_samples')).entries()
                learned=(samples or {}).get(sample,{}).get('hero')
                observation={**p,'hero':learned or p.get('hero'),
                             'hero_source':'hud_verified' if learned else 'hud_portrait'}
                roster.setdefault(key,[]).append(observation)
                slot=p.get('slot')
                candidate=p.get('hero_candidate')
                hero=canonical_hero(candidate) if isinstance(candidate,str) else None
                if owned_roster and type(slot) is int and 0<=slot<=2 and hero and path:
                    candidates.setdefault(key,[]).append((slot,hero,created,str(path).casefold()))
                    slots[slot]=(key,hero)
            if owned_roster and len(players)==3 and len(slots)==3 and path:
                slot_frames.append((created,str(path).casefold(),slots))

        if len(settled_names)==3 and '' not in settled_names:
            full=[frame for frame in slot_frames if {key for key,_ in frame[2].values()}==settled_names]
            for created,path,slots in slot_frames:
                unknown=[slot for slot,(key,_) in slots.items() if key not in settled_names]
                if len(unknown)!=1:
                    continue
                target=unknown[0]
                _,hero=slots[target]
                names={other[target][0] for at,origin,other in full
                       if at!=created and origin!=path and other[target][1]==hero
                       and all(other[slot][0]==slots[slot][0] for slot in slots if slot!=target)}
                if len(names)==1:
                    candidates.setdefault(names.pop(),[]).append((target,hero,created,path))
        for key,sightings in candidates.items():
            heroes={hero for _,hero,_,_ in sightings}
            if len(heroes)!=1:
                continue
            for slot in range(3):
                frames=[(at,path) for position,_,at,path in sightings if position==slot]
                if len({at for at,_ in frames})>=2 and len({path for _,path in frames})>=2:
                    roster[key].insert(0,{'hero':next(iter(heroes)),'hero_source':'hud_consensus'})
                    break
        if verified_rosters:
            summary=match.get('summary')
            if summary is None:
                summary={'kind':'summary','players':[],'placement':None,'squad_kills':None,'warnings':[]}
            current=summary['players']
            known={nickname(p.get('name','')) for p in current}
            best=max(verified_rosters,key=len)
            candidate={nickname(p['name']) for p in best}
            if known<=candidate and len(candidate)<=3:
                for p in best:
                    if nickname(p['name']) not in known:
                        current.append({'name':display_name(p['name']),'name_source':'hud_snapshot'})
                        changed=True
                if changed:
                    match['summary']=summary
                    if match.get('record_source') in (None,'opening','verified_recovery'):
                        match['record_source']='last_snapshot'
        for p in (match.get('summary') or {}).get('players',[]):
            if p.get('hero_source') in ('manual','settlement_visual','hud_visual'):continue
            observations=roster.get(nickname(p.get('name','')),[])
            verified=[o for o in observations if o['hero_source']=='hud_verified']
            consensus=[o for o in observations if o['hero_source']=='hud_consensus']
            if not verified and p.get('hero_source') in ('hud_verified','hud_consensus'):
                continue
            usable=verified or consensus or [o for o in observations if o.get('hero')]
            if len({canonical_hero(o['hero']) for o in usable})>1:continue
            info=(usable or observations or [{}])[0]
            for key in ('hero','hero_sample'):
                value=canonical_hero(info.get(key)) if key=='hero' else info.get(key)
                if value and p.get(key)!=value:p[key]=value;changed=True
            if info.get('hero') and p.get('hero_source')!=info['hero_source']:
                p['hero_source']=info['hero_source'];changed=True
        return changed

    def fill_hud_roster(self,match):
        """Enrich confirmed snapshot rows without replacing final/manual stats."""
        extend=match.get('record_source') in (None,'last_snapshot','verified_recovery')
        return self.attach_heroes(match,add_roster=extend)

    def repair_heroes(self):
        from matchlog.heroes import HeroSamples
        count=0
        with self.lock:
            samples=HeroSamples(media_dir(self.root,'hero_samples')).entries()
            for match in self.all():
                if self.attach_heroes(match,samples,add_roster=match.get('record_source') in
                                      ('last_snapshot','verified_recovery')):
                    self._save(match,commit=False);count+=1
            self.db.commit()
        return count

    def ingest(self, result, image_path, digest=None, timestamp=None, round_id=None, manual=False, fill_only=False):
        now=time.time() if timestamp is None else timestamp
        kind=result['kind']
        with self.lock:
            config=self.settings()
            account=nickname(config['self_name'])
            if not account:return None
            if kind in ('unknown','training','lobby','other','intro','death_recap','rank_overview','postmatch'):
                return None
            if kind=='hud':
                if self.phase in ('rank','summary'):
                    self.active_id=None
                self.phase='hud'
                self.hud={k:v for k,v in result.items() if k!='tokens'}
                return None
            if kind=='profile':return None
            if kind=='rank' and (result.get('delta') is None or result.get('tally_pending')):
                return None
            if kind=='summary' and not result.get('players'):
                return None
            digest=digest or hashlib.sha256(Path(image_path).read_bytes()).hexdigest()
            capture_round=None
            if round_id:
                capture_round=self.db.execute('SELECT match_id,scope,discarded,phase FROM capture_rounds WHERE id=?',(round_id,)).fetchone()
                if not capture_round or capture_round[1]!=self.journal.scope() or capture_round[2]:return None
                digest=hashlib.sha256((round_id+':'+digest).encode()).hexdigest()
            else:
                digest=hashlib.sha256((account+':'+digest).encode()).hexdigest()
            previous=self.db.execute("""SELECT e.match_id FROM evidence e JOIN matches m ON m.id=e.match_id
                WHERE e.hash=? AND json_extract(m.payload,'$.account')=?
                AND json_extract(m.payload,'$.deleted_at') IS NULL""",(digest,account)).fetchone()
            if previous and not fill_only:
                return previous[0]
            match_id=capture_round[0] if capture_round else self.active_id
            match=next((m for m in self.all() if m['id']==match_id),None)
            if match and ((not round_id and now-self.last_ingest>180) or match['season']!=config['season'] or match['split']!=config['split']):
                match=None
            if not match:
                match={'id':uuid.uuid4().hex[:12],'created':now,'time':datetime.fromtimestamp(now,timezone(timedelta(hours=8))).isoformat(),
                       'season':config['season'],'split':config['split'],'account':account,'summary':None,'rank':None,
                       'evidence':[],'corrected':False}
                match.update(mode='ranked' if kind=='rank' else result.get('mode') or self.current_mode(round_id),mode_source='screen')
                if round_id:match['round_id']=round_id
                if capture_round and capture_round[3]=='upload':match['record_origin']='upload'
            payload={k:v for k,v in result.items() if k not in ('tokens','size')}
            if manual and fill_only and kind=='rank' and match.get('mode') in ('casual','event'):
                raise ValueError('该记录为非排位对局，请核对截图和目标对局')
            previous_time=match.get(kind+'_captured_at')
            manual_fields=match.get('manual_fields',{})
            automatic_fill=bool(match.get('corrected') and 'manual_fields' not in match and not manual)
            fill_only=fill_only or automatic_fill
            if match.get(kind) and not manual and not fill_only and previous_time and datetime.fromisoformat(previous_time).timestamp()>now:
                return match['id']
            if kind=='summary':
                if match.get('summary') and not manual and not fill_only and match.get('record_source')!='last_snapshot':
                    from matchlog.completeness import summary_complete
                    if summary_complete(match['summary'],config['self_name']) and not summary_complete(payload,config['self_name']):
                        return match['id']
                    if payload.get('squad_kills') is None and match['summary'].get('squad_kills') is not None:
                        payload['squad_kills']=match['summary']['squad_kills']
                if match.get('summary'):
                    players={nickname(p['name']):p for p in match['summary'].get('players',[]) if p.get('name')}
                    for player in payload['players']:
                        key=nickname(player.get('name',''))
                        old=players.get(key,{})
                        if old.get('hero') and (not player.get('hero') or old.get('hero_source') in ('manual','settlement_visual','hud_visual','hud_verified','hud_consensus')):
                            player.update({key:old[key] for key in ('hero','hero_source','hero_sample') if key in old})
                        for field in manual_fields.get('players',{}).get(key,[]):
                            player[field]=old.get(field)
                    if manual_fields.get('placement'):
                        payload['placement']=match['summary'].get('placement')
                probe={**match,'summary':payload};self.attach_heroes(probe);payload=probe['summary']
            if kind=='rank' and match.get('rp_manual') and match.get('rank'):
                payload['delta']=match['rank'].get('delta')
            badge_anchor={}
            if kind=='rank' and match.get('rank'):
                from matchlog.rp_progress import prefer_badge_anchor
                badge_anchor=prefer_badge_anchor(match['rank'],payload)
            replace_pending=(kind=='rank' and (match.get('rank') or {}).get('tally_pending')
                             and not match.get('rp_manual') and bool(capture_round and capture_round[0]==match['id'])
                             and ((match.get('rank') or {}).get('game_key') in (None,payload.get('game_key')))
                             and (match['rank'].get('placement') in (None,payload.get('placement')))
                             and bool(payload.get('breakdown')))
            if fill_only and match.get(kind) and not replace_pending:
                old=match[kind]
                if not automatic_fill and kind=='rank' and old.get('delta') is not None and payload.get('delta') is not None and old['delta']!=payload['delta']:
                    raise ValueError('截图加减分与已有记录不一致，未补入；请确认选择的是同一局')
                if kind=='summary':
                    if not automatic_fill and old.get('placement') is not None and payload.get('placement') is not None and old['placement']!=payload['placement']:
                        raise ValueError('截图排名与已有记录不一致，未补入')
                    known={nickname(p['name']):p for p in old.get('players',[])}
                    fresh={nickname(p['name']):p for p in payload.get('players',[])}
                    snapshot=match.get('record_source')=='last_snapshot'
                    if not known.keys()<=fresh.keys() and not snapshot:raise ValueError('截图队员与已有记录不一致，未补入')
                    players=[]
                    for name,newer in fresh.items():
                        player=known.get(name,{})
                        if snapshot and not automatic_fill:
                            locked=manual_fields.get('players',{}).get(name,[])
                            player={k:v for k,v in player.items() if k in ('hero','hero_sample','hero_source') or k in locked}
                        if not automatic_fill and any(player.get(k) is not None and newer.get(k) is not None and player[k]!=newer[k] for k in ('kills','assists','knocks','damage')):
                            raise ValueError('截图战绩与已有记录不一致，未补入')
                        players.append({**newer,**{k:v for k,v in player.items() if v is not None}})
                    payload['players']=players
                payload={**payload,**{k:v for k,v in old.items() if v is not None and k not in ('warnings','players')}}
            if badge_anchor:
                payload.update(badge_anchor)
            if kind=='summary':
                from matchlog.recognition import summary_warnings
                payload['warnings']=summary_warnings(payload)
            if kind=='rank' and not match.get('rp_manual'):
                from matchlog.rp_reconcile import verify_rank
                payload['validation']=verify_rank(match,payload)
            # Changing animations produce new pixels but identical fields. Keep one evidence per page.
            if match.get(kind)==payload:
                self.active_id=match['id']
                self.last_ingest=now
                self.phase=kind
                return match['id']
            match[kind]=payload
            if kind=='rank' and payload.get('placement') and match.get('summary') and match['summary'].get('placement') is None:
                match['summary']['placement']=payload['placement']
            if kind=='rank':match.update(mode='ranked',mode_source='rank_page')
            if kind=='summary':match['record_source']='settlement'
            match[kind+'_captured_at']=datetime.fromtimestamp(now,timezone(timedelta(hours=8))).isoformat()
            if not match.get('settled_at') or (not match.get('time_corrected')
                    and now<datetime.fromisoformat(match['settled_at']).timestamp()):
                match['settled_at']=match[kind+'_captured_at']
                if match.get('round_started_at') and not match.get('time_corrected'):
                    match.update(created=now,time=match['settled_at'])
            if match.get('record_source')=='opening' and kind=='rank':match.pop('record_source',None)
            match['updated_at']=match[kind+'_captured_at']
            match['evidence']=[e for e in match['evidence'] if e['kind']!=kind]+[{'kind':kind,'path':str(Path(image_path).resolve())}]
            self._fill_hud_start(match)
            self._save(match,commit=False)
            if round_id:self.db.execute('UPDATE capture_rounds SET match_id=? WHERE id=?',(match['id'],round_id))
            self.db.execute('INSERT OR REPLACE INTO evidence VALUES (?,?,?,?)',(digest,match['id'],kind,str(image_path)))
            self.db.commit()
            self.active_id=match['id']
            self.last_ingest=now
            self.phase=kind
            return match['id']

    def open_round(self,round_id,entry_cost=None,*,map_name=None):
        """Create a shell only after the caller confirms the ranked intro and owner."""
        with self.lock,self.db:
            row=self.db.execute('SELECT started,scope,phase,discarded,match_id FROM capture_rounds WHERE id=?',
                                (round_id,)).fetchone()
            if not row or row[1]!=self.journal.scope() or row[2] in ('alias','upload','training') or row[3]:return None
            at,_,_,_,ident=row
            cfg=self.settings();account=nickname(cfg['self_name'])
            if not account:return None
            stamp=datetime.fromtimestamp(at,timezone(timedelta(hours=8))).isoformat()
            if ident:
                match=next((m for m in self.all() if m['id']==ident),None)
                if not match:return None
                match['round_started_at']=stamp
            else:
                ident=uuid.uuid4().hex[:12]
                match={'id':ident,'created':at,'time':stamp,'round_started_at':stamp,'round_id':round_id,
                       'season':cfg['season'],'split':cfg['split'],'account':account,'mode':'ranked',
                       'mode_source':'screen','record_source':'opening','summary':None,'rank':None,
                       'evidence':[],'corrected':False}
            match['round_start_source']='intro'
            if type(entry_cost) is int and 0<=entry_cost<=300:match['entry_cost_at_start']=entry_cost
            if isinstance(map_name,str) and 0<len(map_name)<=40 and not match.get('map_name'):
                match['map_name']=map_name
            self._save(match,commit=False)
            self.db.execute("UPDATE capture_rounds SET match_id=?,mode='ranked' WHERE id=?",(ident,round_id))
            return ident

    def _fill_hud_start(self, match):
        """Use the earliest verified own HUD when the match opening was missed."""
        rid=match.get('round_id')
        if (not rid or match.get('time_corrected') or match.get('record_origin')=='upload'
                or match.get('round_started_at') and match.get('round_start_source')!='hud'):
            return False
        end=match.get('settled_at') or match.get('snapshot_at')
        try:
            if not end:
                known_end=[stamp for field in ('summary_captured_at','rank_captured_at') if (stamp:=match.get(field))]
                end=min(known_end,key=lambda stamp:datetime.fromisoformat(stamp).timestamp(),default=None)
            if not end:return False
            ended=datetime.fromisoformat(end).timestamp()
        except (TypeError,ValueError,OverflowError):
            return False
        scope=json.dumps([match['season'],match['split'],match.get('account')],ensure_ascii=False)
        at,=self.db.execute("""SELECT MIN(h.created) FROM hud_backups h
            JOIN capture_rounds r ON r.id=json_extract(h.payload,'$.round_id')
            WHERE r.id=? AND r.scope=? AND r.discarded=0 AND r.phase NOT IN ('alias','upload','training')
              AND json_extract(h.payload,'$.identity')='self' AND json_extract(h.payload,'$.account')=?
              AND (r.game_key IS NULL OR json_extract(h.payload,'$.game_key') IS NULL
                   OR r.game_key=json_extract(h.payload,'$.game_key'))
              AND h.created BETWEEN ? AND ?""",
            (rid,scope,match.get('account'),ended-3600,ended)).fetchone()
        if at is None:return False
        stamp=datetime.fromtimestamp(at,timezone(timedelta(hours=8))).isoformat()
        old=match.get('round_started_at')
        try:
            if old and datetime.fromisoformat(old).timestamp()<=at:return False
        except (TypeError,ValueError,OverflowError):
            pass
        match.update(round_started_at=stamp,round_start_source='hud')
        if not match.get('settled_at') and not match.get('snapshot_at'):match['settled_at']=end
        return True

    def repair_hud_start_times(self):
        with self.lock,self.db:
            key=f'hud_start_v1:{self.journal.scope()}'
            if self.db.execute('SELECT 1 FROM recorder_flags WHERE name=?',(key,)).fetchone():return 0
            repaired=0
            for match in self.all():
                if self._fill_hud_start(match):
                    self._save(match,commit=False)
                    repaired+=1
            self.db.execute('INSERT INTO recorder_flags VALUES (?,1)',(key,))
            return repaired

    def edit(self, match_id, patch):
        with self.lock:
            match=next((m for m in self.all() if m['id']==match_id),None)
            if not match:
                raise ValueError('对局不存在')
            legacy_locks=match.get('corrected') and 'manual_fields' not in match
            locks=match.get('manual_fields',{})
            if not legacy_locks:match['manual_fields']=locks
            if 'time' in patch:
                try:
                    at=datetime.fromisoformat(str(patch['time']))
                    if at.tzinfo is None:raise ValueError()
                except (ValueError,TypeError):raise ValueError('请输入有效的北京时间') from None
                match.update(created=at.timestamp(),time=at.astimezone(timezone(timedelta(hours=8))).isoformat(),
                             time_corrected=True,rp_order_at=at.timestamp())
            if 'delta' in patch:
                match['rank']=match['rank'] or {'kind':'rank','warnings':[]}
                value=patch['delta']
                if value not in ('',None) and match.get('mode') in ('casual','event'):
                    raise ValueError('只有排位对局可以填写 RP')
                match['rank']['delta']=None if value in ('',None) else int(value)
                if match['rank']['delta'] is not None and abs(match['rank']['delta'])>3000:
                    raise ValueError('单局分数超出范围')
                if match['rank']['delta'] is not None and match.get('mode')=='unknown':match.update(mode='ranked',mode_source='manual_rp')
                match['rp_manual']=True
            if 'placement' in patch and not match.get('summary'):
                match['summary']={'kind':'summary','players':[{'name':self.settings()['self_name']}],'warnings':[]}
            if 'placement' in patch and match['summary']:
                place=patch['placement']
                place=None if place in ('',None) else int(place)
                if place is not None and not 1<=place<=30:
                    raise ValueError('排名需为 1–30')
                if match['summary'].get('placement')!=place:locks['placement']=True
                match['summary']['placement']=place
            if 'players' in patch and match['summary']:
                current=match['summary']['players']
                if len(patch['players'])!=len(current):
                    raise ValueError('队员数量不一致')
                for player,updates in zip(current,patch['players']):
                    previous=player.copy()
                    old_key=nickname(previous['name'])
                    if 'name' in updates:
                        player['name']=str(updates['name']).strip()[:64]
                    if 'hero' in updates:
                        player['hero']=canonical_hero(str(updates['hero'] or '').strip()[:40]) or None
                        if player['hero']!=previous.get('hero'):player['hero_source']='manual'
                    for key in ('kills','assists','knocks','participation','damage','deaths','revives','respawns'):
                        if key in updates:
                            value=updates[key]
                            value=None if value in ('',None) else int(value)
                            if value is not None and not 0<=value<=100000:
                                raise ValueError('战绩数字超出范围')
                            player[key]=value
                    changed={key for key in updates if previous.get(key)!=player.get(key)}
                    players_locks=locks.setdefault('players',{})
                    previous_locks=set(players_locks.pop(old_key,[]))
                    if changed or previous_locks:
                        players_locks[nickname(player['name'])]=sorted(changed|previous_locks)
                if any('name' in player for player in patch['players']):
                    validate_roster(current, self.settings()['self_name'])
            match['corrected']=True
            if match.get('summary'):
                from matchlog.recognition import summary_warnings
                match['summary']['warnings']=summary_warnings(match['summary'])
            self._save(match)
            return match

    def create_manual(self, patch):
        cfg=self.settings()
        try:
            at=datetime.fromisoformat(str(patch.get('time','')))
            if at.tzinfo is None:raise ValueError()
        except (ValueError,TypeError):raise ValueError('请输入对局时间') from None
        mode=patch.get('mode','ranked')
        if mode not in ('ranked','casual','event'):raise ValueError('请选择对局模式')
        players=patch.get('players') or [{'name':cfg['self_name']}]
        names=validate_roster(players,cfg['self_name'])
        match={'id':uuid.uuid4().hex[:12],'created':at.timestamp(),'time':at.astimezone(timezone(timedelta(hours=8))).isoformat(),
               'season':cfg['season'],'split':cfg['split'],'account':nickname(cfg['self_name']),
               'mode':mode,'mode_source':'manual','record_origin':'manual',
               'record_source':'manual','time_corrected':True,'corrected':True,'manual_fields':{},'rank':None,'evidence':[],
               'summary':{'kind':'summary','placement':None,'expected_players':len(names),
                          'players':[{'name':n} for n in names],'warnings':[]}}
        with self.lock:
            self._save(match)
            try:
                self.edit(match['id'],{'players':players,**{k:patch[k] for k in ('placement','delta') if k in patch}})
                self.journal.upload_round(match['id'])
            except Exception:
                self.db.execute('DELETE FROM matches WHERE id=?',(match['id'],));self.db.commit();raise
        return next(m for m in self.all() if m['id']==match['id'])

    def delete(self, match_id):
        with self.lock:
            match=next((m for m in self.all() if m['id']==match_id),None)
            if not match:raise ValueError('对局不存在')
            self.db.execute('UPDATE capture_rounds SET discarded=1 WHERE match_id=?',(match_id,))
            self.db.execute("UPDATE capture_jobs SET status='dismissed' WHERE round_id IN (SELECT id FROM capture_rounds WHERE match_id=?)",(match_id,))
            match['deleted_at']=time.time()
            self._save(match,commit=False)
            self.db.commit()
            if self.active_id==match_id:
                self.active_id=None

    def restore(self, match_id):
        with self.lock:
            row=self.db.execute('SELECT payload FROM matches WHERE id=?',(match_id,)).fetchone()
            match=json.loads(row[0]) if row else {}
            if not match.get('deleted_at') or match.get('account')!=nickname(self.settings()['self_name']):
                raise ValueError('没有可撤销的对局')
            match.pop('deleted_at')
            self._save(match,commit=False)
            self.db.execute('UPDATE capture_rounds SET discarded=0 WHERE match_id=?',(match_id,))
            self.db.commit()

    def save_hud_backup(self, payload, image_path, timestamp=None):
        now=time.time() if timestamp is None else timestamp
        with self.lock:
            cfg=self.settings();ident=uuid.uuid4().hex[:12]
            if payload.get('round_id') and not self.db.execute('SELECT 1 FROM capture_rounds WHERE id=? AND scope=?',
                       (payload['round_id'],self.journal.scope())).fetchone():return None
            snapshot={k:payload.get(k) for k in ('kills','assists','participation','damage','live_rp')}
            snapshot['players']=[{'name':str(p.get('name',''))[:64],'hero':p.get('hero'),
                                  'hero_source':p.get('hero_source'),'hero_sample':p.get('hero_sample'),'slot':p.get('slot'),
                                  **({'hero_candidate':p['hero_candidate']} if p.get('hero_candidate') else {})}
                                 for p in payload.get('players',[])[:3]]
            snapshot.update(id=ident,time=datetime.fromtimestamp(now,timezone(timedelta(hours=8))).isoformat(),
                            season=cfg['season'],split=cfg['split'],account=nickname(cfg['self_name']),source='hud',
                            verified=False,identity=payload.get('identity'),game_key=payload.get('game_key'),round_id=payload.get('round_id'))
            if payload.get('squad_path'):snapshot['squad_path']=str(payload['squad_path'])
            self.db.execute('INSERT INTO hud_backups VALUES (?,?,?,?,?,?,NULL)',
                (ident,now,cfg['season'],cfg['split'],json.dumps(snapshot,ensure_ascii=False),str(Path(image_path).resolve())))
            if snapshot['round_id'] and snapshot['identity']=='self':
                linked=self.db.execute('SELECT match_id FROM capture_rounds WHERE id=?',(snapshot['round_id'],)).fetchone()
                if linked and linked[0]:
                    match=next((m for m in self.all() if m['id']==linked[0]),None)
                    if match:
                        self.db.execute('UPDATE hud_backups SET match_id=? WHERE id=?',(match['id'],ident))
                        start_changed=self._fill_hud_start(match)
                        if self.fill_hud_roster(match) or start_changed:self._save(match,commit=False)
            self.db.commit();self.hud=snapshot
            return snapshot

    def hud_backups(self, limit=20):
        with self.lock:
            cfg=self.settings()
            rows=self.db.execute('SELECT payload,path,match_id FROM hud_backups WHERE season=? AND split=? AND '+self._hud_owner_sql()
                                 +' ORDER BY created DESC LIMIT ?',
                                 (cfg['season'],cfg['split'],nickname(cfg['self_name']),self.journal.scope(),limit)).fetchall()
            return [{**json.loads(row[0]),'path':row[1],'match_id':row[2]} for row in rows]

    @staticmethod
    def _hud_owner_sql():
        return """(json_extract(payload,'$.account')=? OR (json_extract(payload,'$.account') IS NULL
                    AND json_extract(payload,'$.round_id') IN (SELECT id FROM capture_rounds WHERE scope=?)))"""

    def backup_image(self, ident):
        with self.lock:
            row=self.db.execute('SELECT path FROM hud_backups WHERE id=? AND '+self._hud_owner_sql(),
                                (str(ident),nickname(self.settings()['self_name']),self.journal.scope())).fetchone()
            if not row:raise ValueError('局内快照不存在')
            return Path(row[0])

    def link_latest_backup(self, match_id, timestamp=None, round_id=None):
        now=time.time() if timestamp is None else timestamp
        with self.lock:
            cfg=self.settings()
            # Only suggest an association for the latest recent screenshot; never promote its values.
            rows=self.db.execute('SELECT id,payload FROM hud_backups WHERE season=? AND split=? AND created BETWEEN ? AND ? AND match_id IS NULL AND '
                +self._hud_owner_sql()+' ORDER BY created DESC LIMIT 10',
                (cfg['season'],cfg['split'],now-180,now,nickname(cfg['self_name']),self.journal.scope())).fetchall()
            row=next((r for r in rows if not round_id or json.loads(r[1]).get('round_id')==round_id),None)
            if row:
                self.db.execute('UPDATE hud_backups SET match_id=? WHERE id=?',(match_id,row[0]));self.db.commit()

    def save_lobby_rank(self, payload, path, timestamp=None):
        now=time.time() if timestamp is None else timestamp
        with self.lock:
            config=self.settings();scope='|'.join([config['season'],config['split'],nickname(config['self_name'])])
            value={**payload,'captured_at':datetime.fromtimestamp(now,timezone(timedelta(hours=8))).isoformat(),
                   'source':'lobby','season':config['season'],'split':config['split']}
            self.db.execute('INSERT INTO lobby_rank VALUES (?,?,?,?) ON CONFLICT(scope) DO UPDATE SET created=excluded.created,payload=excluded.payload,path=excluded.path WHERE excluded.created>=lobby_rank.created',
                (scope,now,json.dumps(value,ensure_ascii=False),str(Path(path).resolve())))
            self.db.commit();return value

    def latest_lobby_rank(self):
        with self.lock:
            c=self.settings();scope='|'.join([c['season'],c['split'],nickname(c['self_name'])])
            row=self.db.execute('SELECT payload FROM lobby_rank WHERE scope=?',(scope,)).fetchone()
            return json.loads(row[0]) if row else None

    def rank_progress(self, matches=None):
        from matchlog.rp_progress import build_progress
        with self.lock:
            cfg=self.settings();scope='|'.join([cfg['season'],cfg['split'],nickname(cfg['self_name'])])
            checks=[{**json.loads(payload),'created':created} for created,payload in self.db.execute(
                'SELECT created,payload FROM rp_checks WHERE scope=?',(scope,))]
            checks.extend({**json.loads(payload),'created':created,'source':'lobby'} for created,payload in self.db.execute(
                'SELECT created,payload FROM lobby_history WHERE scope=?',(self.journal.scope(),)))
            lobby=self.latest_lobby_rank()
            if lobby:checks.append(lobby)
            unique={((c.get('captured_at') or c.get('created')),c.get('total_rp'),c.get('rank'),c.get('progress')):c for c in checks}
            matches=self.all() if matches is None else matches
            matches=[m for m in matches if (m['season'],m['split'])==(cfg['season'],cfg['split'])]
            return build_progress(cfg,matches,list(unique.values()))

    def calibrate_rp(self, value):
        value=int(value)
        if not 0<=value<=2000000:raise ValueError('总 RP 超出范围')
        with self.lock:
            cfg=self.settings();scope='|'.join([cfg['season'],cfg['split'],nickname(cfg['self_name'])])
            payload={'total_rp':value,'source':'manual','label':'手动校准'}
            self.db.execute('INSERT INTO rp_checks VALUES (?,?,?,?)',(uuid.uuid4().hex,scope,time.time(),json.dumps(payload)))
            self.db.commit()

    def season_climb(self, current, records=None):
        from matchlog.rp_progress import build_progress
        cfg=self.settings();own=nickname(cfg['self_name']);season=cfg['season']
        matches=[m for m in (self.all() if records is None else records) if m['season']==season]
        splits={m['split'] for m in matches}|{cfg['split']};points=[]
        for split in splits:
            if split==cfg['split']:progress=current
            else:
                checks=[{**json.loads(raw),'created':at} for at,raw in self.db.execute('SELECT created,payload FROM rp_checks WHERE scope=?',('|'.join((season,split,own)),))]
                checks.extend({**json.loads(raw),'created':at,'source':'lobby'} for at,raw in self.db.execute('SELECT created,payload FROM lobby_history WHERE scope=?',(json.dumps([season,split,own],ensure_ascii=False),)))
                progress=build_progress({**cfg,'split':split,'initial_rp':None,'initial_rp_at':None},[m for m in matches if m['split']==split],checks)
            points.extend({**point,'split':split} for point in progress['points'])
        return sorted(points,key=lambda p:p['at'])

    def save_profile(self, payload, path, timestamp=None):
        if nickname(payload.get('name',''))!=nickname(self.settings()['self_name']):return None
        now=time.time() if timestamp is None else timestamp
        with self.lock:
            cfg=self.settings();scope=nickname(cfg['self_name'])
            clean={k:v for k,v in payload.items() if k not in ('tokens','size')}
            last=self.db.execute('SELECT payload FROM profile_snapshots WHERE scope=? ORDER BY created DESC LIMIT 1',(scope,)).fetchone()
            if last and json.loads(last[0])==clean:return False
            self.db.execute('INSERT INTO profile_snapshots VALUES (?,?,?,?,?)',
                (uuid.uuid4().hex,scope,now,json.dumps(clean,ensure_ascii=False),str(Path(path).resolve())))
            self.db.commit();return True

    def latest_profile(self):
        with self.lock:
            rows=self.db.execute('SELECT created,payload FROM profile_snapshots WHERE scope=? ORDER BY created DESC LIMIT 30',
                (nickname(self.settings()['self_name']),)).fetchall()
            if not rows:return None
            result=json.loads(rows[0][1]);cached={}
            for at,raw in rows[1:]:
                previous=json.loads(raw)
                if previous.get('mode_label')!=result.get('mode_label'):continue
                for section in ('lifetime','season'):
                    if section=='season' and previous.get('selected_season')!=result.get('selected_season'):continue
                    for key,value in (previous.get(section) or {}).items():
                        if key in ('approximate_fields','display_values') or value is None:continue
                        if (result.get(section) or {}).get(key) is None:
                            result.setdefault(section,{})[key]=value;cached[section+'.'+key]=at
                            shown=(previous.get(section) or {}).get('display_values',{}).get(key)
                            if shown:result[section].setdefault('display_values',{})[key]=shown
            return {**result,'cached_fields':cached,'captured_at':datetime.fromtimestamp(rows[0][0],timezone(timedelta(hours=8))).isoformat()}

    def finalize_snapshot(self, backup):
        """Promote the last HUD as a labeled fallback; never invent squad combat or RP."""
        now=datetime.fromisoformat(backup['time']).timestamp();rid=backup.get('round_id')
        if not rid or backup.get('identity') not in (None,'self'):return None
        with self.lock:
            row=self.db.execute("SELECT match_id,discarded,scope FROM capture_rounds WHERE id=? AND phase!='alias'",(rid,)).fetchone()
            if not row or row[1] or row[2]!=self.journal.scope():return None
            cfg=self.settings();match=next((m for m in self.all() if m['id']==row[0]),None)
            if match and ((match.get('corrected') and 'manual_fields' not in match)
                          or (match.get('summary') and match.get('record_source') not in ('last_snapshot','opening'))):
                return None
            if not match:
                match={'id':uuid.uuid4().hex[:12],'created':now,'time':backup['time'],'season':cfg['season'],'split':cfg['split'],
                       'round_id':rid,'account':nickname(cfg['self_name']),'mode':self.current_mode(rid),
                       'mode_source':'screen','summary':None,'rank':None,'evidence':[],'corrected':False}
            if match.get('_snapshot_created',0)>=now:return None
            old_summary=match.get('summary') or {}
            old_players={nickname(p['name']):p for p in old_summary.get('players',[]) if p.get('name')}
            players=[dict(p) for p in old_players.values()]
            for p in backup.get('players',[]):
                if nickname(p['name']) not in old_players:
                    players.append({'name':display_name(p['name']),'name_source':'hud_snapshot',
                                    **{key:p[key] for key in ('hero','hero_source','hero_sample') if p.get(key)}})
            own=next((p for p in players if nickname(p['name'])==nickname(cfg['self_name'])),None)
            if own is None:
                own={'name':cfg['self_name']};players.append(own)
            locked=match.get('manual_fields',{}).get('players',{}).get(nickname(cfg['self_name']),[])
            for field in ('kills','assists','participation','damage'):
                if backup.get(field) is not None and field not in locked:
                    own[field]=backup[field]
                    own[field+'_source']='hud_snapshot'
            known_roster=backup.get('identity')=='self' and any(nickname(p['name'])==nickname(cfg['self_name'])
                                                               for p in backup.get('players',[]))
            if not known_roster and not any(own.get(k) is not None for k in ('kills','assists','damage')):return None
            match.update(summary={**old_summary,'kind':'summary','players':players[:3],
                                  'placement':old_summary.get('placement'),'squad_kills':None,'warnings':[]},
                         record_source='last_snapshot',snapshot_at=backup['time'],_snapshot_created=now,snapshot_backup_id=backup['id'])
            from matchlog.hud_score import plausible_hud_rp
            if backup.get('identity')=='self' and plausible_hud_rp(backup.get('live_rp'),match.get('entry_cost_at_start')):
                match.update(snapshot_live_rp=backup['live_rp'],snapshot_rp_verified=True)
            self.attach_heroes(match)
            self._fill_hud_start(match)
            self._save(match,commit=False)
            self.db.execute('UPDATE capture_rounds SET match_id=? WHERE id=?',(match['id'],rid))
            self.db.execute('UPDATE hud_backups SET match_id=? WHERE id=?',(match['id'],backup['id']))
            self.db.commit();return match['id']


def validate_roster(players, self_name):
    if not 1<=len(players)<=3:raise ValueError('队伍最多三人')
    names=[display_name(str(p.get('name','')).strip()[:64]) for p in players]
    keys={nickname(name) for name in names}
    if '' in keys or len(keys)!=len(names) or nickname(self_name) not in keys:
        raise ValueError('请填写不重复的玩家昵称，并包含自己')
    return names


def teammate_stats(matches, config):
    result={}
    friends={nickname(x) for x in config['friends']}
    for match in matches:
        summary=match.get('summary')
        if not summary:
            continue
        own=next((p for p in summary['players'] if nickname(p['name'])==nickname(config['self_name'])),None)
        if not own:
            continue
        for p in summary['players']:
            key=nickname(p['name'])
            if key==nickname(config['self_name']) or key not in friends:
                continue
            row=result.setdefault(key,{'name':display_name(p['name']),'fixed':key in friends,'games':0,'wins':0,'damage':[],
                                       'kills':[],'assists':[],'knocks':[],'my_rp':0,'rp_games':0})
            row['games']+=1
            row['wins']+=summary.get('placement')==1
            for field in ('damage','kills','assists','knocks'):
                if p.get(field) is not None:
                    row[field].append(p[field])
            delta=(match.get('rank') or {}).get('delta')
            if delta is not None and match.get('mode','ranked')=='ranked':
                row['my_rp']+=delta
                row['rp_games']+=1
    for name in config['friends']:
        key=nickname(name)
        result.setdefault(key,{'name':name,'fixed':True,'games':0,'wins':0,'damage':[],
                               'kills':[],'assists':[],'knocks':[],'my_rp':0,'rp_games':0})
    for row in result.values():
        for field in ('damage','kills','assists','knocks'):
            values=row[field]
            row[field]=round(sum(values)/len(values),1) if values else None
    return sorted(result.values(),key=lambda r:(not r['fixed'],-r['games']))
