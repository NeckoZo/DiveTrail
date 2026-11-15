"""Apex Toolbox desktop entry point. All match data stays in ./data."""
from pathlib import Path
import base64
import csv
import hashlib
import json
import logging
import threading
import time
from datetime import datetime,timezone,timedelta
from matchlog.season_calendar import SeasonCalendar,aware_time

from matchlog.recognition import Recognizer,nickname,display_name
from matchlog.storage import Store, teammate_stats
from matchlog.notifications import Notice, saved_notice
from matchlog.settlement_sounds import SettlementSounds
from matchlog.map_rotation import MapRotation
from matchlog.media_paths import existing_media_path, media_dir, resolve_media_path
from matchlog.capture_diagnostics import CaptureDiagnostics
from matchlog.capture_journal import frame_asset, queued_frame
from matchlog.capture_sampling import HudTail
from matchlog.highlights import highlight_preview, merged_highlight_previews

ROOT=Path(__file__).resolve().parent
APP_NAME='DiveTrail · 尾迹'


class Api:
    _auto_switch_enabled = False

    def __init__(self, data=None, desktop=True):
        self._store=Store(data or ROOT/'data')
        self._journal=self._store.journal
        self._ocr=Recognizer(hero_samples=media_dir(self._store.root,'hero_samples'))
        self._window=None
        self._title=APP_NAME
        self._desktop=desktop
        self._portable=False
        self._stop=threading.Event()
        self._requested=threading.Event()
        self._manual_lock=threading.Lock()
        self._manual_deadline=None
        self._manual_window=None
        self._diagnostics=CaptureDiagnostics(limit=400,folder=self._store.root/'logs' if desktop else None)
        self._job_reasons={}
        self._processing=threading.Lock()
        self._running=bool(desktop)
        self._runtime={'message':'准备就绪 · 等待 Apex','size':None,'last':None,'error':False,'hotkey':'F8','location':'待识别'}
        self._scout_count=0
        self._hud_candidate_count=0
        self._hud_tail_reads=0
        self._capture_reads=0
        self._debug_evidence={}
        self._debug_evidence_lock=threading.Lock()
        self._debug_game=None
        self._debug_queue_count=0
        self._debug_queue_checked=0
        self._debug_round_info=(None,None,None)
        self._recognition_updated=0
        self._candidate=None
        self._candidate_count=0
        from matchlog.summary_navigation import SummaryNavigator
        self._navigator=SummaryNavigator()
        self._settlement_sounds=SettlementSounds(self._store.root,ROOT/'ui/assets/sounds')
        self._map_rotation=MapRotation()
        self._profile_notifier=None
        self._round_complete=False
        self._last_hud_backup=None
        self._last_lobby=None
        self._debugger=None
        self._launched_at=time.time()
        self._calendar=SeasonCalendar(self._store.root)
        self._calendar_checked=0
        self._queued_games={}
        self._confirmed_views={}
        self._completed_rounds=set()
        self._last_capture_hint=None
        self._last_capture_seen=-float('inf')
        self._last_pre_match=None
        self._last_selection_probe=None
        self._last_queued_hud=None
        self._hud_sampling={}
        self._hud_tail=HudTail()
        self._settlement_sampling={}
        self._training_seen=None
        self._last_queued_lobby=None
        self._parsed_frames={}
        self._last_capture_error=None
        self._last_profile=None
        self._last_fallback_check=0
        self._snapshot_notices=set()
        self._last_cleanup=-float('inf')
        self._reset_nonce=None
        self._repair_checked=0
        self._settlement_verify_until={}
        self._startup_audit={'state':'queued','message':'等待核对过去24小时'}
        self._audit_queue=None
        self._pending_import=None
        self._profile_scan=None
        self._lobby_name_sighting=None
        self._merge_preview=None
        self._requested_retries=[]
        self._transition_reads={}
        self._queued_openings=set()
        self._release_version=json.loads((ROOT/'ui/changelog.json').read_text(encoding='utf-8'))['version']
        from matchlog.release_updates import ReleaseUpdates
        self._updates=ReleaseUpdates(self._release_version)
        from matchlog.community import Community
        self._community=Community(self._store.root)
        from matchlog.cloud_log_admin import CloudLogAdmin
        self._cloud_log_admin=CloudLogAdmin(self._store.root, ROOT)
        from matchlog.artwork import Artwork
        self._artwork=Artwork(self._store.root/'artwork/renders', ROOT/'ui/assets/heroes/skins')

    def hero_gallery(self):return self._artwork.gallery()

    def capture_diagnostics(self,match_id=None):
        if match_id is not None:
            from matchlog.capture_trace import timeline
            return timeline(self._store,match_id)
        return self._diagnostics.report()

    def cloud_log_admin_status(self):
        return self._cloud_log_admin.status()

    def export_cloud_diagnostics(self, pin=None):
        import webview
        from matchlog.cloud_log_admin import query_cloud_rows, write_export
        if not self._window:
            raise ValueError('请在 DiveTrail 窗口下载日志。')
        command=self._cloud_log_admin.authorize(pin)
        stamp=datetime.now(timezone(timedelta(hours=8))).strftime('%y%m%d-%H%M%S')
        chosen=self._window.create_file_dialog(webview.FileDialog.SAVE,
            save_filename=f'divetrail-cloud-logs-{stamp}.jsonl',file_types=('JSONL (*.jsonl)',))
        if not chosen:return {'ok':False}
        path=Path(chosen[0] if isinstance(chosen,(tuple,list)) else chosen)
        if path.suffix.lower()!='.jsonl':path=path.with_suffix('.jsonl')
        return write_export(path,lambda sql:query_cloud_rows(command,sql))

    def preview_diagnostics_upload(self, scope, feedback=''):
        return self._community.preview_diagnostics(self._diagnostics, scope, self._release_version, feedback)

    def upload_diagnostics(self, token):
        return self._community.upload_diagnostics(token)

    def community_status(self):
        return self._community.status()

    def set_community_enabled(self, enabled):
        return self._community.set_enabled(enabled)

    def preview_community_upload(self):
        with self._store.lock:
            owner=self._store.settings()['self_name']
            matches=self._store.all()
        return self._community.preview(matches,owner)

    def upload_community_matches(self, token):
        with self._store.lock:
            owner=self._store.settings()['self_name']
            matches=self._store.all()
        return self._community.upload(token,owner,matches)

    def community_summary(self):
        cfg=self._store.settings()
        return self._community.aggregate(cfg['season'],cfg['split'],'ranked')

    def save_share_card(self, encoded):
        from io import BytesIO
        from PIL import Image
        import webview
        if not self._window or not isinstance(encoded,str) or len(encoded)>4_000_000:
            raise ValueError('分享图大小无效')
        with Image.open(BytesIO(base64.b64decode(encoded,validate=True)),formats=['PNG']) as source:
            if source.size!=(900,480):
                raise ValueError('分享图尺寸无效')
            card=source.convert('RGB')
        chosen=self._window.create_file_dialog(webview.FileDialog.SAVE,
            save_filename='DiveTrail-今晚战绩.png',file_types=('PNG (*.png)',))
        if not chosen:return {'ok':False}
        path=chosen[0] if isinstance(chosen,(tuple,list)) else chosen
        card.save(path,format='PNG')
        return {'ok':True}

    def export_capture_diagnostics(self, display=None,match_id=None):
        import webview
        from matchlog.diagnostic_export import CHINA, build_report, report_filename, write_report
        if not self._window:
            raise ValueError('请在 DiveTrail 窗口导出诊断')
        stamp = datetime.now(CHINA).strftime('%y%m%d-%H%M%S')
        chosen = self._window.create_file_dialog(
            webview.FileDialog.SAVE,
            save_filename=report_filename(self._store.settings()['self_name'],stamp,single_match=match_id is not None),
            file_types=('诊断报告 (*.json)',),
        )
        if not chosen:
            return {'ok': False}
        path = chosen[0] if isinstance(chosen, (tuple, list)) else chosen
        runtime={**self._runtime,'capture_counts':{
            'probe_frames':self._scout_count,
            'hud_memory_candidates':self._hud_candidate_count,
            'processed_jobs':self._capture_reads,
            'hud_tail_attempts':self._hud_tail_reads,
        }}
        report = build_report(
            self._store, self._diagnostics, runtime, display, version=self._release_version,
            running=self._running, portable=self._portable,match_id=match_id,
        )
        write_report(path, report)
        return {'ok': True}

    def hero_artwork(self, ident):return self._artwork.image(ident)

    def create_match(self, patch):
        with self._processing:self._store.create_manual(patch)
        return self.state()

    def preview_merge_matches(self,ids):
        from matchlog.record_merge import preview
        with self._store.lock:
            self._merge_preview,result=preview(self._store,ids)
            return result

    def confirm_merge_matches(self,token,target_id,acknowledged=False):
        from matchlog.record_merge import confirm
        if not self._processing.acquire(timeout=3):
            raise ValueError('正在识别截图，请稍后再确认合并')
        try:
            with self._store.lock:
                confirm(self._store,self._merge_preview,token,target_id,acknowledged)
                self._merge_preview=None
        finally:
            self._processing.release()
        return self.state()

    def undo_merge_matches(self):
        from matchlog.record_merge import undo_last
        with self._processing,self._store.lock:
            undo_last(self._store)
            return self.state()

    def begin_profile_scan(self):
        if not self._desktop:
            raise ValueError('请在 Windows 桌面版识别游戏昵称')
        with self._store.lock:
            if nickname(self._store.settings()['self_name']):
                raise ValueError('已建立本机档案，可在设置里修改昵称')
            self._profile_scan={'deadline':time.monotonic()+45,'next':0,'last':None}
            self._runtime['profile_scan']={'status':'scanning'}
            self._diagnostics.add('profile_waiting',phase='lobby')
            return self._runtime['profile_scan']

    def cancel_profile_scan(self):
        with self._store.lock:
            self._profile_scan=None
            self._runtime.pop('profile_scan',None)

    def confirm_initial_profile(self, name):
        if not isinstance(name,str) or not name.strip() or not nickname(name):
            raise ValueError('请填写 Apex 游戏昵称')
        with self._processing,self._store.lock:
            if nickname(self._store.settings()['self_name']):
                raise ValueError('本机档案已建立')
            candidate=(self._profile_scan or {}).get('candidate')
            self.settings({'self_name':name})
            self.cancel_profile_scan()
            if self._desktop:
                self._running=True
                self._journal.set_running(True)
            config=self._store.settings()
            if (candidate and candidate['rank'].get('progress') is not None and time.monotonic()<candidate['expires']
                    and nickname(candidate['name'])==nickname(config['self_name'])
                    and candidate['season']==config['season'] and candidate['split']==config['split']):
                try:
                    self._save_lobby(None,candidate['rank'],force=True,captured_at=candidate['captured_at'],
                                     rank_crop=candidate['crop'])
                except OSError:
                    logging.warning('could not save first-run lobby crop',exc_info=True)
            self._last_queued_lobby=None
            self._last_capture_hint=None
            return self.state()

    def _scan_profile_step(self):
        from matchlog.recognition import lobby_crop
        from matchlog.rp_progress import rank_total
        from matchlog.windows_capture import capture_frame,capture_lobby_probe,capture_scout,foreground_game
        now=time.monotonic()
        with self._store.lock:
            scan=self._profile_scan
            if not scan:
                return
            candidate=scan.get('candidate')
            if not candidate and now>=scan['deadline'] and scan.get('sighting'):
                image,observed=scan['sighting']
                rank={}
                config=self._store.settings()
                scan['candidate']={**observed,'rank':rank,'crop':lobby_crop(image),'captured_at':time.time(),
                                   'season':config['season'],'split':config['split'],'expires':now+120}
                self._runtime['profile_scan']={'status':'candidate','name':observed['name']}
                self._profile_tip('读到候选昵称','请返回 DiveTrail 核对后确认。',scan.get('game'))
                return
            if now>=(candidate['expires'] if candidate else scan['deadline']):
                self._profile_scan=None
                self._runtime['profile_scan']={'status':'expired'}
                self._diagnostics.add('profile_timeout',phase='lobby')
                self._profile_tip('未能确认游戏昵称','请返回 DiveTrail 重试或手动填写游戏昵称。',scan.get('game'),tone='warning')
                return
            if candidate or now<scan['next']:
                return
            scan['next']=now+1.5
        observed=None
        stage='waiting_game'
        reason=None
        if game:=foreground_game():
            stage='finding_lobby'
            try:
                top,bottom,scouted=capture_scout(include_bottom=False)
                if scouted==game:
                    size=(game['width'],game['height'])
                    hint=self._ocr.screen_hint_regions(top,bottom,size)
                    if hint=='lobby_candidate':
                        bottom=capture_lobby_probe(game)
                        confirmed=self._ocr.screen_hint_regions(top,bottom,size)
                        if confirmed!='other':hint=confirmed
                    if hint in ('lobby','lobby_candidate'):
                        stage='reading'
                        self._runtime['profile_scan']={'status':stage,'remaining_seconds':max(1,int(scan['deadline']-now))}
                        image,captured=capture_frame()
                        if captured==game:
                            observed=self._ocr.read_ranked_lobby_identity(image,verified_lobby=hint=='lobby',
                                                                         candidate=hint=='lobby_candidate')
            except (RuntimeError,OSError) as error:
                reason='暂时读不到游戏画面，请让排位大厅保持在前台。'
                self._diagnostics.add('black_frame' if '黑屏' in str(error) else 'focus_lost',phase='lobby',every=5)
            except Exception:
                logging.warning('first-run lobby recognition failed',exc_info=True)
        with self._store.lock:
            if scan is not self._profile_scan or time.monotonic()>=scan['deadline']:
                return
            config=self._store.settings()
            if nickname(config['self_name']):
                self.cancel_profile_scan()
                return
            if game:
                scan['game']=game
            if not observed:
                status='wrong_mode' if stage=='reading' and scan.get('warned_mode') else stage
                self._runtime['profile_scan']={'status':status,'reason':reason,'remaining_seconds':max(1,int(scan['deadline']-now))}
                self._diagnostics.add({'waiting_game':'profile_waiting','finding_lobby':'profile_lobby',
                                       'reading':'profile_name_missing'}[stage],phase='lobby',every=4)
                return
            if observed.get('mode') in ('casual','event'):
                scan['last']=None
                scan.pop('sighting',None)
                if not scan.get('warned_mode'):
                    scan['warned_mode']=True
                    self._runtime['profile_scan']={'status':'wrong_mode'}
                    self._diagnostics.add('profile_mode',phase='lobby')
                    self._profile_tip('请切换到排位赛','在 Apex 左下角选择「排位赛」，识别会继续。',game,tone='warning')
                return
            rank=observed.get('rank') or {}
            identity=nickname(observed['name'])
            scan['sighting']=(image,observed)
            if identity!=scan['last'] or observed.get('candidate'):
                scan['last']=identity
                scan['last_rank']=rank
                self._runtime['profile_scan']={'status':'confirming','remaining_seconds':max(1,int(scan['deadline']-now))}
                self._diagnostics.add('profile_confirming',phase='lobby')
                return
            last=scan['last_rank']
            if any(rank.get(key)!=last.get(key) for key in ('rank','progress','capacity')):
                # The name may be confirmed before the rank animation settles.
                rank={'mode':'ranked'}
            scan['candidate']={**observed,'season':config['season'],'split':config['split'],
                               'rank':rank,
                               'crop':lobby_crop(image),'captured_at':time.time(),'expires':time.monotonic()+120}
            self._runtime['profile_scan']={'status':'ready','name':observed['name'],'rank':rank.get('rank'),
                                           'progress':rank.get('progress'),'capacity':rank.get('capacity'),
                                           'total_rp':rank_total(rank,config['season'])}
            self._diagnostics.add('profile_ready',phase='lobby')
            self._profile_tip('已识别游戏昵称','请返回 DiveTrail，核对昵称后开始记录。',game,tone='success')

    def _profile_tip(self, title, detail, game=None, tone='info'):
        if self._profile_notifier:
            self._profile_notifier.show(Notice(title,detail,tone,9),game)

    def _check_lobby_profile(self, image, captured_at, game=None):
        """Do not attribute a live lobby rank to a different known player."""
        current=self._store.settings()['self_name']
        name=self._ocr.read_lobby_name(image,expanded=True)
        if nickname(self._store.settings()['self_name'])!=nickname(current):
            return False
        pending=self._runtime.get('profile_change')
        if not name:
            self._lobby_name_sighting=None
            return not pending
        owner,key=nickname(current),nickname(name)
        if owner==key:
            self._lobby_name_sighting=None
            self._runtime.pop('profile_change',None)
            return True
        if pending and nickname(pending['detected'])==key:
            self._lobby_name_sighting=None
            return False
        previous=self._lobby_name_sighting
        self._lobby_name_sighting=(key,captured_at)
        if previous and previous[0]==key and .3<=captured_at-previous[1]<=15:
            self._lobby_name_sighting=None
            self._runtime['profile_change']={'current':current,'detected':name}
            self._runtime.update(message='检测到新昵称 · 等待选择档案',error=False)
            self._profile_tip('游戏昵称有变化','请返回 DiveTrail，选择切换或合并档案。',game)
        return False

    def resolve_profile_change(self, action):
        if action not in ('new','merge'):
            raise ValueError('请选择新档案或合并历史战绩')
        from matchlog.profile_rename import rename_profile
        with self._processing,self._store.lock:
            change=self._runtime.get('profile_change')
            if not change:
                raise ValueError('没有待确认的昵称变更')
            rename_profile(self._store,change['detected'],merge=action=='merge',expected_name=change['current'])
            self._runtime.pop('profile_change',None)
            self._lobby_name_sighting=None
            self._last_queued_lobby=None
            self._last_queued_hud=None
            self._last_profile=None
            self._last_capture_hint=None
            self._queued_games.clear()
            self._audit_queue=None
            self._startup_audit={'state':'queued','message':'等待核对过去24小时'}
            self._candidate=None
            self._candidate_count=0
            self._round_complete=False
            self._runtime.update(message='昵称已更新 · 等待 Apex 大厅',error=False)
            return self.state()

    def _note_capture(self, round_id, evidence):
        if not round_id:return
        with self._debug_evidence_lock:
            self._debug_evidence.setdefault(round_id,set()).add(evidence)
            if len(self._debug_evidence)>40:
                self._debug_evidence.pop(next(iter(self._debug_evidence)))

    def _debug(self, stage, detail='', game=None):
        self._runtime['stage']=stage
        if not (self._debugger and self._debugger.debug_enabled and self._running):return
        if game:self._debug_game=game
        phase=self._runtime.get('game_phase')
        phase_label={'no_game':'等待 Apex 前台','lobby':'大厅','profile':'个人资料','training':'靶场',
                     'hud':'对局中','spectator':'观战','settlement':'结算','postmatch':'赛后界面','intro':'对局中（开局）',
                     'legend_select':'对局中（选人）','loading':'载入对局'}.get(phase,'画面待确认')
        if phase in ('intro','legend_select','loading','hud','settlement','spectator'):
            location=self._runtime.get('location')
            if location in ('排位','匹配','活动'):phase_label=f'{location} · {phase_label}'
        now=time.monotonic()
        round_id=self._runtime.get('capture_round_id')
        if now-self._debug_queue_checked>=1 and self._store.lock.acquire(blocking=False):
            try:
                self._debug_queue_count=self._store.db.execute(
                    """SELECT COUNT(*) FROM capture_jobs j JOIN capture_rounds r ON r.id=j.round_id
                       WHERE j.scope=? AND r.discarded=0 AND
                       (j.status='pending' OR (j.status='deferred' AND j.kind='hud'
                       AND json_extract(j.payload,'$._hud_managed')=1
                       AND j.round_id=? AND j.created>=?))""",
                    (self._journal.scope(),round_id,time.time()-86400),
                ).fetchone()[0]+len(self._requested_retries)
                row=self._store.db.execute(
                    '''SELECT json_extract(m.payload,'$.round_started_at'),json_extract(m.payload,'$.map_name')
                       FROM capture_rounds r JOIN matches m ON m.id=r.match_id
                       WHERE r.id=? AND r.discarded=0''',
                    (round_id,),
                ).fetchone() if round_id else None
                self._debug_round_info=(round_id,*(row or (None,None)))
                self._debug_queue_checked=now
            finally:
                self._store.lock.release()
        software=f'待识别 {self._debug_queue_count} 张' if self._debug_queue_count else '采集就绪'
        work=self._visible_recognition() or {}
        activity={'reading':'识别中','awaiting_frame':'等待结算画面'}.get(work.get('stage'))
        if activity:
            software=f'{activity} · {software}'
        evidence=''
        if round_id and phase in ('intro','legend_select','loading','hud','settlement','spectator','postmatch'):
            cached_round,start,map_name=self._debug_round_info
            if cached_round!=round_id:
                start=map_name=None
            with self._debug_evidence_lock:
                hud='√' if 'hud' in self._debug_evidence.get(round_id,set()) else '—'
            opened='—'
            if start:
                try:opened=aware_time(start).astimezone(timezone(timedelta(hours=8))).strftime('%H:%M:%S')
                except (TypeError,ValueError,AttributeError):pass
            evidence=f'HUD {hud}  ·  开局 {opened}  ·  {map_name or "地图待识别"}'
        self._debugger.show(Notice(phase_label,software,'debug',8,evidence,lobby=phase=='lobby'),game or self._debug_game)

    def acknowledge_whats_new(self,version):
        if version==self._release_version:
            with self._store.lock,self._store.db:
                self._store.db.execute("INSERT OR REPLACE INTO recorder_flags VALUES ('whats_new_seen',?)",(version,))

    def _whats_new(self):
        row=self._store.db.execute("SELECT value FROM recorder_flags WHERE name='whats_new_seen'").fetchone()
        return self._release_version if not row or row[0]!=self._release_version else None

    def _sync_calendar(self, force=False):
        if not force and time.monotonic()-self._calendar_checked<30:return
        self._calendar_checked=time.monotonic()
        cfg=self._store.settings()
        if not cfg['auto_season'] or self._calendar.error:return
        event=self._calendar.due()
        if event and event['at']!=self._calendar.data.get('last_applied'):
            self._store.configure({'season':event['season'],'split':event['split']})
            self._calendar.replace({**self._calendar.data,'last_applied':event['at']})
            self._candidate=None;self._candidate_count=0;self._round_complete=False
            self._runtime.update(message='已按北京时间表切换到 '+event['season']+' / '+event['split'],error=False)

    def _cached(self, timestamp):
        try:return (timestamp if isinstance(timestamp,(int,float)) else aware_time(timestamp).timestamp())<self._launched_at
        except (ValueError,TypeError,AttributeError):return True

    def _settlement_sound(self,event,round_id,game,captured_at=None):
        if not self._desktop or not self._running or not round_id or not game:
            return False
        if captured_at is not None and not 0<=time.time()-captured_at<=20:
            return False
        config=self._store.settings()
        if not config.get(f'sound_{event}_enabled'):
            return False
        return self._settlement_sounds.play(config['settlement_tone'],round_id=round_id,event=event)

    def map_rotation(self):
        return self._map_rotation.fetch()

    def open_map_source(self):
        if self._desktop:
            import webbrowser
            webbrowser.open('https://apexlegendsstatus.com/current-map')
        return {'ok':self._desktop}

    def open_website(self):
        from matchlog.release_updates import WEBSITE
        return self._open_release_link(WEBSITE)

    def open_releases(self):
        from matchlog.release_updates import RELEASES
        return self._open_release_link(RELEASES+'/latest')

    def _open_release_link(self,url):
        if not self._desktop:return {'ok':False}
        import webbrowser
        return {'ok':webbrowser.open(url)}

    def check_release_update(self):
        return self._updates.check() if self._desktop else {'status':'unavailable'}

    def state(self):
        with self._store.lock:
            return self._state_snapshot()

    def poll_state(self, revision=None, include_recovery=False):
        with self._store.lock:
            self._sync_calendar()
            # Periodically recheck rolling windows, file retention and Windows
            # startup settings even if the database has not changed.
            external=self._store.db.execute('PRAGMA data_version').fetchone()[0]
            current=':'.join(map(str,(
                self._store.dashboard_revision(),external,int(time.time()//15),self._whats_new(),
                hash(json.dumps(self._calendar.data,sort_keys=True)),self._startup_audit.get('state'),
            )))
            if revision==current:
                live={'runtime':self._runtime_snapshot(),'running':self._running}
                if include_recovery:
                    live['recovery']={'capture_inbox':self._journal.public(),'hud_backups':self._public_hud_backups()}
                return {'revision':current,'live':live}
            return {'revision':current,'state':self._state_snapshot()}

    def _visible_recognition(self):
        work=self._runtime.get('recognition')
        if work and work['stage']=='awaiting_frame':
            if (self._runtime.get('game_phase') not in ('settlement','postmatch')
                    or self._runtime.get('capture_round_id')!=work.get('round_id')
                    or time.monotonic()-self._recognition_updated>=12):
                return None
        return work

    def _runtime_snapshot(self):
        runtime=dict(self._runtime)
        if (work:=self._visible_recognition()) is None:
            runtime.pop('recognition',None)
        else:
            runtime['recognition']=work
        return runtime

    def _state_snapshot(self):
        self._sync_calendar()
        config=self._store.settings()
        all_records=self._store.all();matches=[m for m in all_records if m['season']==config['season'] and m['split']==config['split']]
        active_round=self._journal.current_round() or {}
        def ongoing(m):
            return (m.get('record_source')=='opening' and m['round_id']==active_round.get('id')
                    and active_round.get('phase') in ('intro','hud') and time.time()-m['created']<3600)
        progress=self._store.rank_progress(matches)
        from matchlog.rp_reconcile import apply_review
        from matchlog.player_stats import player_hero_stats
        from matchlog.hud_score import hud_score_evidence
        matches=[apply_review(m,progress['review'].get(m['id'])) for m in matches]
        hud_scores=hud_score_evidence(self._store,matches,config)
        # Return no local filesystem paths to the renderer. Evidence is loaded on demand.
        public=[]
        from matchlog.completeness import record_status
        for m in matches:
            stamp=m.get('rank_captured_at') or m.get('time')
            row={**m,'mode':m.get('mode','unknown'),'mode_source':m.get('mode_source','default'),
                 'evidence':[item for item,path in self._evidence_media(m) if path],
                 'cache':{'historical':self._cached(stamp),'captured_at':stamp}}
            row['status']=record_status(m,config['self_name'])
            if m['id'] in hud_scores:row['hud_rp_evidence']=hud_scores[m['id']]
            if ongoing(m):
                row['status']={'code':'ongoing','complete':False,'label':'进行中','missing':[]}
            if m.get('audit'):row['audit']={**m['audit'],'missing':row['status']['missing']}
            public.append(row)
        lobby=self._store.latest_lobby_rank()
        if lobby:lobby={**{k:v for k,v in lobby.items() if k not in ('tokens','size')},'cache':{'historical':self._cached(lobby['captured_at']),'captured_at':lobby['captured_at']}}
        if progress['current']:
            progress['current']['cache']={'historical':self._cached(progress['current']['at']),'captured_at':datetime.fromtimestamp(progress['current']['at'],timezone(timedelta(hours=8))).isoformat()}
        profile=self._store.latest_profile()
        from matchlog.windows_startup import status
        startup=status(ROOT) if self._desktop and not self._portable else {'enabled':False,'supported':False}
        audit=dict(self._startup_audit)
        if audit.get('state')=='done':audit['issues']=[{'id':m['id'],'missing':m['audit']['missing']} for m in public if m.get('audit',{}).get('missing') and 0<=time.time()-m['created']<=86400]
        finished=[m for m in matches if not ongoing(m)]
        return {'config':config,'matches':public,'teammates':teammate_stats(finished,config),
                'player_hero_stats':player_hero_stats(finished,config),'profile_isolation':self._store.profile_isolation(),
                'paths':{name:str(path) for name,path in self._file_folders().items()},
                'running':self._running,'runtime':self._runtime_snapshot(),'hud':self._store.hud,'whats_new':self._whats_new(),
                'can_undo_merge':bool(self._store.db.execute("SELECT 1 FROM recorder_flags WHERE name='manual_merge_undo'").fetchone()),
                'lobby_rank':lobby,
                'calendar':self._calendar.status(),'startup':startup,
                'capture_inbox':self._journal.public(),'lobby_samples':self._journal.lobby_samples(),
                'rank_progress':progress,'climb_points':self._store.season_climb(progress,[m for m in all_records if not ongoing(m)]),'profile':profile,'record_audit':audit,
                'hud_backups':self._public_hud_backups()}

    def _public_hud_backups(self):
        return [{**{k:v for k,v in row.items() if k not in ('path','squad_path')},
                 'has_image':bool(existing_media_path(self._store.root,row['path']))}
                for row in self._store.hud_backups()]

    def calibrate_rp(self, total):
        self._store.calibrate_rp(total)
        return self.state()

    def cleanup_images(self, mode='temporary'):
        from matchlog.capture_cleanup import clean_images
        with self._processing:result=clean_images(self._store,mode)
        return {**result,'state':self.state()}

    def _file_folders(self):
        root=self._store.root.resolve()
        return {'screenshots':media_dir(root,'captures').parent,'highlights':media_dir(root,'highlights'),
                'inbox':media_dir(root,'inbox'),'backups':root/'backups','skins':root/'artwork/renders','data':root}

    def open_folder(self,kind='screenshots'):
        path=self._file_folders().get(kind)
        if path is None:raise ValueError('目录不存在')
        path.mkdir(parents=True,exist_ok=True)
        import os
        if self._desktop:os.startfile(path)
        return {'ok':True}

    def export_backup(self):
        import webview
        from matchlog.store_backup import export_backup
        folder=self._store.root/'backups';folder.mkdir(exist_ok=True)
        chosen=self._window.create_file_dialog(webview.FileDialog.SAVE,directory=str(folder),
            save_filename='divetrail-'+datetime.now(timezone(timedelta(hours=8))).strftime('%y%m%d-%H%M')+'.matchlog',file_types=('DiveTrail 备份 (*.matchlog)',))
        if not chosen:return {'ok':False}
        path=Path(chosen[0] if isinstance(chosen,(tuple,list)) else chosen)
        if path.suffix.lower()!='.matchlog':path=path.with_suffix('.matchlog')
        with self._processing:result=export_backup(self._store,path)
        return {'ok':True,**result}

    def import_backup(self):
        import webview
        from matchlog.store_backup import import_backup
        files=self._window.create_file_dialog(webview.FileDialog.OPEN,file_types=('DiveTrail 备份 (*.matchlog)',))
        if not files:return {'ok':False}
        with self._processing:
            result=import_backup(self._store,files[0])
            if result.get('schedule'):
                with self._store.lock:
                    self._calendar=SeasonCalendar(self._store.root)
                    self._calendar_checked=0
            self._store.repair_heroes()
        return {'ok':True,**result,'state':self.state()}

    def prepare_reset(self):
        import secrets
        token=secrets.token_urlsafe(24);self._reset_nonce=(token,time.monotonic())
        return {'token':token,'matches':self._store.match_count_all_accounts()}

    def reset_data(self,token):
        value=self._reset_nonce;self._reset_nonce=None
        if not value or token!=value[0] or time.monotonic()-value[1]>120:raise ValueError('请重新确认清空数据')
        from matchlog.capture_cleanup import FOLDERS
        from matchlog.media_paths import media_dirs
        from matchlog.migrations import backup_database
        cleanup_failures=0
        with self._processing,self._store.lock:
            if self._store.match_count_all_accounts():backup_database(self._store,'before-reset')
            config=self._store.settings()
            config.update(initial_rp=None,initial_rp_at=None)
            with self._store.db:
                for table in ('matches','evidence','hud_backups','lobby_rank','rp_checks','profile_snapshots','capture_jobs','capture_rounds','lobby_history','match_highlights','recorder_flags','media_asset_paths','capture_trace','recognition_retries'):
                    self._store.db.execute(f'DELETE FROM {table}')
                self._store.db.execute('INSERT OR REPLACE INTO settings VALUES (1,?)',(json.dumps(config,ensure_ascii=False),))
                self._store.db.execute("INSERT INTO recorder_flags VALUES ('snapshot_fallback_since',?)",(int(time.time()),))
            self._store.active_id=None;self._store.phase=None;self._store.hud=None
            root=self._store.root.resolve()
            folders={folder for name in (*FOLDERS,'highlights') for folder in media_dirs(root,name)}
            for folder in folders:
                if not folder.is_dir() or folder.is_symlink():continue
                for path in folder.rglob('*'):
                    safe=existing_media_path(root,path)
                    if not safe:continue
                    try:safe.unlink(missing_ok=True)
                    except OSError:cleanup_failures+=1
            self._last_queued_hud=None;self._last_queued_lobby=None;self._last_profile=None
            self._hud_sampling.clear()
            self._hud_tail.clear()
            self._settlement_sampling.clear()
            self._training_seen=None
            self._runtime.pop('profile_change',None)
            self._lobby_name_sighting=None
            self._candidate=None;self._candidate_count=0;self._confirmed_views.clear();self._parsed_frames.clear();self._completed_rounds.clear()
            self._journal.set_running(self._running)
        return {**self.state(),'reset_cleanup_errors':cleanup_failures}

    def window_control(self,command):
        if not self._window:return {'ok':False}
        if command=='minimize':self._window.minimize()
        elif command=='maximize':
            if getattr(self,'_maximized',False):self._window.restore()
            else:self._window.maximize()
        elif command=='close':
            if getattr(self,'_tray',None):self._window.hide()
            else:self._window.destroy()
        else:raise ValueError('窗口操作不正确')
        return {'ok':True}

    def window_sidebar_minimum(self, viewport_height, pixel_ratio):
        from matchlog.window_frame import set_sidebar_minimum
        return {'ok':bool(self._window and set_sidebar_minimum(self._window,viewport_height,pixel_ratio))}

    def _sync_highlight(self,match):
        from matchlog.highlights import sync_highlight
        if match:sync_highlight(self._store,match)

    def settings(self, patch):
        if self._portable:
            patch={**patch,'start_on_login':False}
        previous=self._store.settings()
        self._store.configure(patch)
        if not self._portable and 'start_on_login' in patch and bool(patch['start_on_login'])!=previous['start_on_login']:
            try:
                if not self._desktop:raise RuntimeError('开机自启请在桌面版设置')
                from matchlog.windows_startup import set_enabled
                set_enabled(ROOT,bool(patch['start_on_login']))
            except Exception:
                self._store.restore_settings(previous)
                raise
        config=self._store.settings()
        from matchlog.config import RETENTION_LIMITS
        if any(config[key]!=previous[key] for key in (*RETENTION_LIMITS,'auto_cleanup')):
            self._last_cleanup=-float('inf')
        if any(config[k]!=previous[k] for k in ('self_name','season','split')):
            self._hud_tail.clear()
            self._audit_queue=None
            self._startup_audit={'state':'queued','message':'等待核对过去24小时'}
            self._candidate=None
            self._candidate_count=0
        if nickname(config['self_name'])!=nickname(previous['self_name']):
            self._runtime.pop('profile_change',None)
            self._lobby_name_sighting=None
            self._last_queued_lobby=None
        if self._debugger:self._debugger.configure(config)
        if config['debug_mode'] and not previous['debug_mode']:
            self._debug('调试模式已开启','右侧显示采集步骤；游戏优先时局内隐藏')
        self._sync_calendar(force=True)
        return self.state()

    def schedule_transition(self, season, split, beijing_time):
        self._calendar.add_local(season,split,beijing_time)
        self._sync_calendar(force=True)
        return self.state()

    def import_schedule(self):
        import webview
        files=self._window.create_file_dialog(webview.FileDialog.OPEN,file_types=('JSON (*.json)',))
        if not files:return {'ok':False}
        self._calendar.replace(json.loads(Path(files[0]).read_text(encoding='utf-8')))
        self._sync_calendar(force=True)
        return {'ok':True}

    def _set_recording(self,running):
        if self._stop.is_set() or self._running==running:return
        self._running=running
        if not self._running:self._hud_tail.clear()
        if self._debugger:self._debugger.suspend(not self._running)
        self._journal.set_running(self._running)
        self._diagnostics.add('resumed' if self._running else 'paused')
        self._runtime.update(message='正在等待 Apex 前台结算画面' if self._running else '已暂停自动采集 · 快捷键仍可使用',error=False)
        self._debug('正在检测' if self._running else '采集已暂停')

    def toggle(self):
        self._set_recording(not self._running)
        return self.state()

    def capture_now(self):
        self._queue_manual_capture()
        self._runtime.update(message='已请求截图，请回到 Apex 窗口',error=False)
        return {'ok':True}

    def _queue_manual_capture(self, game=None):
        with self._manual_lock:
            self._manual_deadline=time.monotonic()+10
            self._manual_window=game
            self._requested.set()
        self._diagnostics.add('manual_requested',phase='manual')

    def _take_manual_capture(self, game):
        """A toolbar request may wait briefly. F8 belongs to its original window."""
        with self._manual_lock:
            if not self._requested.is_set():return False
            expired=self._manual_deadline is not None and time.monotonic()>self._manual_deadline
            changed=self._manual_window is not None and self._manual_window!=game
            if not game and not (expired or changed):return False
            self._requested.clear()
            self._manual_deadline=self._manual_window=None
            if expired or changed:
                message='补拍请求已过期' if expired else '游戏窗口已切换，请重新按 F8'
                self._runtime.update(stage=message,error=False)
                self._diagnostics.add('manual_expired' if expired else 'focus_lost',phase='manual')
                return False
            return True

    def _hotkey_request(self):
        from matchlog.windows_capture import foreground_game
        game=foreground_game()
        if not game:
            self._runtime.update(message='请切到 Apex 后再按 F8',stage='快捷键未采集',error=False)
            self._diagnostics.add('no_game',phase='manual',every=5)
            return
        self._queue_manual_capture(game)

    def preview_settlement_sound(self,tone):
        return {'ok':self._desktop and self._settlement_sounds.play(tone)}

    def backup_evidence(self, ident):
        path=resolve_media_path(self._store.root,self._store.backup_image(ident))
        if not path.is_file():raise ValueError('截图已清理')
        return ('data:image/webp;base64,' if path.suffix.lower()=='.webp' else 'data:image/png;base64,')+base64.b64encode(path.read_bytes()).decode()

    def _save_hud_backup(self, image, result=None, force=False, now=None, captured_at=None, round_id=None, repair=False,
                         roster_frame=True, managed_path=None, tail=False):
        now=time.monotonic() if now is None else now
        if not force and self._last_hud_backup is not None and now-self._last_hud_backup<30:
            return None
        self._last_hud_backup=now
        scope=self._store.settings()
        self._debug('正在识别局内保底','读取局内数字'+('和三人头像' if roster_frame else ''))
        result=result or self._ocr.read_hud(image)
        result={**result,'players':self._ocr.read_squad(image,self_name=scope['self_name'],roster_frame=roster_frame),'round_id':round_id}
        if repair:
            from matchlog.profile_rename import relabel_historical_result
            result=relabel_historical_result(self._store,{**result,'kind':'hud'},round_id,captured_at)
        roster=result['players']
        owner=next((p for p in roster if p.get('slot',2)==2 and nickname(p['name'])==nickname(scope['self_name'])),None)
        if owner is None:
            watched=next((nickname(p['name']) for p in roster if p.get('slot')==2 and nickname(p['name'])),None)
            sample=self._hud_sampling.get(round_id)
            if watched and sample and round_id==self._runtime.get('capture_round_id') and not repair:
                previous=sample.get('watched')
                sample['watched']=watched
                if watched==previous or watched in {nickname(name) for name in scope['friends']}:
                    sample['spectator_until']=time.monotonic()+120
                    self._hud_tail.freeze(self._journal.scope(),round_id,captured_at or time.time())
                    self._runtime.update(location='观战',game_phase='spectator')
            return None
        sample=self._hud_sampling.get(round_id)
        if sample and not repair:
            sample.pop('watched',None)
            sample.pop('spectator_until',None)
        result['identity']='self'
        if tail:
            # A final candidate was captured before the boundary. It cannot
            # create a new round or overwrite a verified settlement footer.
            if not (round_id:=self._journal.canonical_round(round_id)):return None
            key=self._journal.key_for_round(round_id)
            if key and result.get('game_key') not in (None,key):return None
            result['game_key']=key
        elif repair:
            # A stale spectator/transition frame cannot create a new historical round.
            linked=self._store.db.execute('SELECT match_id FROM capture_rounds WHERE id=? AND discarded=0',(round_id,)).fetchone()
            if not linked or not linked[0]:return None
            keys=self._journal.game_keys(linked[0])
            if keys and result.get('game_key') and result['game_key'] not in keys:return None
        elif round_id:
            round_id=self._journal.bind_game_key(round_id,result.get('game_key'),captured_at or time.time(),
                                                observation=result)
            if not round_id:return None
            result['game_key']=self._journal.key_for_round(round_id)
        result['round_id']=round_id
        current=self._store.settings()
        if any(scope[k]!=current[k] for k in ('season','split','self_name')):return None
        if tail and not any(result.get(k) is not None for k in ('kills','assists','damage','live_rp')):return None
        from matchlog.recognition import hud_crop,squad_crop
        from matchlog.image_storage import save_archive,timestamp_path
        folder=media_dir(self._store.root,'hud')
        at=time.time() if captured_at is None else captured_at
        if tail or managed_path:
            return self._persist_managed_hud(image,result,at,managed_path,tail)
        path=save_archive(hud_crop(image),timestamp_path(folder,'hud',at))
        squad_path=save_archive(squad_crop(image),timestamp_path(folder,'hud-squad',at)) if roster_frame else None
        if squad_path:result['squad_path']=squad_path
        saved=self._store.save_hud_backup(result,path,timestamp=captured_at)
        if not saved:
            path.unlink(missing_ok=True)
            if squad_path:squad_path.unlink(missing_ok=True)
            return None
        self._note_capture(saved.get('round_id'),'hud')
        self._note_capture(saved.get('round_id'),'start')
        self._runtime.update(message='局内快照已保存',error=False)
        return saved

    def _persist_managed_hud(self, image, result, at, path, tail):
        rid=result['round_id']
        with self._store.lock:
            old=self._store.db.execute("""SELECT payload FROM hud_backups
                WHERE created=? AND json_extract(payload,'$.round_id')=?
                  AND json_extract(payload,'$.identity')='self' LIMIT 1""",(at,rid)).fetchone()
            if old:return json.loads(old[0])
            job=None
            if tail and not path:
                job=self._journal.enqueue(image,'hud',rid,timestamp=at,hud_roster=False,
                                          client_px=image.size,managed_hud=True,hud_tail=True)
                if not job:return None
                path=self._journal.get(job)['path']
            saved=self._store.save_hud_backup(result,path,timestamp=at)
            if job:self._journal.finish(job,'done' if saved else 'review')
        if saved:
            self._note_capture(rid,'hud')
            self._note_capture(rid,'start')
            self._runtime.update(message='局内快照已保存',error=False)
        return saved

    def _save_lobby(self, image, result=None, force=False, captured_at=None, round_id=None, rank_crop=None):
        if not force and self._last_lobby is not None and time.monotonic()-self._last_lobby<120:return None
        self._last_lobby=time.monotonic()
        self._debug('正在读取大厅排位','检测左下角排位卡片')
        cfg=self._store.settings();result=result or self._ocr.read_lobby(image)
        current=self._store.settings()
        if any(cfg[k]!=current[k] for k in ('season','split','self_name')):return None
        if result.get('mode'):self._store.detect_mode(result['mode'],source='lobby',timestamp=captured_at)
        if not result.get('rank') or result.get('progress') is None:return {'mode':result['mode']} if result.get('mode') else None
        prior=self._store.latest_lobby_rank()
        if prior and prior.get('rank_anchor_status')=='badge' and result.get('rank_anchor_status')!='badge':
            from matchlog.rp_progress import rank_total, stamp
            before,after=rank_total(prior,cfg['season']),rank_total(result,cfg['season'])
            same_tier=str(prior.get('rank','')).split()[0]==str(result['rank']).split()[0]
            at=time.time() if captured_at is None else captured_at
            if same_tier and before is not None and after is not None and abs(before-after)>=500 and 0<=at-stamp(prior['captured_at'])<=180:
                return None
        from matchlog.recognition import lobby_crop
        from matchlog.image_storage import save_archive,timestamp_path
        folder=media_dir(self._store.root,'lobby')
        path=save_archive(rank_crop if rank_crop is not None else lobby_crop(image),timestamp_path(folder,'lobby',captured_at))
        saved=self._store.save_lobby_rank(result,path,timestamp=captured_at)
        self._journal.save_lobby(saved,path,round_id=round_id,timestamp=captured_at)
        self._runtime.update(message='大厅排位已更新 · '+result['rank'],error=False)
        self._debug('大厅排位已保存',f'{result["rank"]} · {result["progress"]}/{result["capacity"]} RP')
        return saved

    def import_image(self, match_id=None):
        from secrets import token_urlsafe
        from matchlog.import_images import ImportPage, MAX_IMAGES, check_image_dimensions, image_digest, plan_import
        import webview
        paths=self._window.create_file_dialog(webview.FileDialog.OPEN,allow_multiple=True,
                    file_types=('图片 (*.png;*.jpg;*.jpeg;*.bmp;*.webp)',))
        if not paths:
            return {'ok':False,'message':'未选择截图'}
        with self._processing:
            self._pending_import=None
            if not nickname(self._store.settings()['self_name']):raise ValueError('请先在设置里填写游戏昵称')
            if len(paths)>MAX_IMAGES:raise ValueError(f'一次最多选择 {MAX_IMAGES} 张截图')
            scope=self._journal.scope()
            pages=[]
            for path in paths:
                path=Path(path)
                digest=image_digest(path)
                try:
                    check_image_dimensions(path)
                    result=self._ocr.read(path)
                except (OSError,ValueError):
                    raise ValueError(f'无法读取截图：{path.name}') from None
                pages.append(ImportPage(path,digest,result))
            groups=plan_import(pages,self._journal.match_for_game_key,match_id,
                               self._journal.game_keys(match_id) if match_id else ())
            if scope!=self._journal.scope():raise ValueError('档案或赛段已切换，请重新选择截图')
            if len(paths)==1:return self._apply_image_import(groups,scope)
            token=token_urlsafe(24)
            self._pending_import=(token,time.monotonic(),scope,groups)
            return {'needs_confirmation':True,'token':token,'groups':[g.preview() for g in groups],
                    'message':'相同场次标识的截图已归组。无法确认是同一局的截图分别保存。'}

    def confirm_image_import(self, token):
        with self._processing:
            pending=self._pending_import
            if not pending or pending[0]!=token:raise ValueError('请重新选择截图')
            self._pending_import=None
            if time.monotonic()-pending[1]>300:raise ValueError('预览已过期，请重新选择截图')
            return self._apply_image_import(pending[3],pending[2])

    def _apply_image_import(self, groups, scope):
        from matchlog.import_images import MATCH_PAGES, image_digest
        if scope!=self._journal.scope():raise ValueError('档案或赛段已切换，请重新选择截图')
        for group in groups:
            for page in group.pages:
                if image_digest(page.path)!=page.digest:raise ValueError(f'截图在预览后已变化：{page.path.name}')
        outcomes=[]
        for group in groups:
            if scope!=self._journal.scope():raise ValueError('档案或赛段已切换，请重新选择截图')
            # Validate the target again because the user may have changed it while previewing.
            if group.target_id and group.game_key:
                current_keys=self._journal.game_keys(group.target_id)
                if current_keys and group.game_key not in current_keys:raise ValueError('目标对局场次已变化，请重新选择')
            if group.pages and group.pages[0].result['kind'] in MATCH_PAGES:
                rid,at=self._journal.upload_round(group.target_id,group.game_key)
            else:rid,at=None,time.time()
            for page in group.pages:
                try:
                    outcome=self._process_file(page.path,manual=True,round_id=rid,captured_at=at,
                                               fill_only=bool(group.target_id),recognized=page.result)
                except ValueError as error:
                    outcome={'kind':page.result['kind'],'saved':False,'warnings':[str(error)]}
                outcomes.append(outcome)
        return {'ok':True,'results':outcomes}

    def _evidence_media(self, match):
        store=self._store
        for index,evidence in enumerate(match['evidence']):
            kind=evidence['kind']
            path=existing_media_path(store.root,evidence['path'])
            if not path and not match.get('preserve_merged_highlights'):
                path=highlight_preview(store,match['id'],kind)
            yield {'kind':kind,'index':index},path
        for owner,kind,stamp,path in merged_highlight_previews(store,match):
            yield {'kind':kind,'index':'highlight:'+owner,'highlight_at':stamp},path

    def evidence(self, match_id, index):
        from PIL import Image
        import io

        with self._store.lock:
            match=next((m for m in self._store.all() if m['id']==match_id),None)
            if not match:
                raise ValueError('截图不存在')
            path=next((path for item,path in self._evidence_media(match) if str(item['index'])==str(index)),None)
            if not path:
                raise ValueError('截图不存在或已清理')
            try:
                with Image.open(path) as image:
                    im=image.convert('RGB')
            except OSError as error:
                raise ValueError('截图无法读取') from error
        im.thumbnail((1920,1200))
        buf=io.BytesIO()
        im.save(buf,format='JPEG',quality=90)
        return 'data:image/jpeg;base64,'+base64.b64encode(buf.getvalue()).decode()

    def edit(self, match_id, patch):
        match=self._store.edit(match_id,patch)
        # An explicit hero correction labels only the face crop linked to this player.
        for player,updates in zip((match.get('summary') or {}).get('players',[]),patch.get('players',[])):
            if updates.get('hero') and player.get('hero_sample'):
                self._ocr.confirm_hero_sample(player['hero_sample'],updates['hero'])
        self._sync_highlight(match)
        return self.state()

    def delete(self, match_id):
        with self._processing:self._store.delete(match_id)
        return self.state()

    def restore_match(self, match_id):
        with self._processing:self._store.restore(match_id)
        return self.state()

    def capture_evidence(self, ident):
        job=self._journal.get(ident)
        if job['scope']!=self._journal.scope():raise ValueError('请先切换到该线索的赛季和账号')
        path=resolve_media_path(self._store.root,job['path'])
        if not path.is_file():raise ValueError('截图已清理')
        return ('data:image/webp;base64,' if path.suffix.lower()=='.webp' else 'data:image/png;base64,')+base64.b64encode(path.read_bytes()).decode()

    def recover_capture(self, ident):
        with self._processing:
            job=self._journal.get(ident)
            if job['scope']!=self._journal.scope():raise ValueError('请先切换到该线索的赛季和账号')
            if job['status'] in ('done','dismissed'):raise ValueError('这张线索已经处理')
            outcome=self._run_capture_job(job,confirm=True)
        return {'ok':bool(outcome.get('saved') or outcome.get('backup') or outcome.get('lobby') or outcome.get('profile')),'state':self.state()}

    def retry_saved_captures(self,match_id=None):
        with self._store.lock:
            cfg=self._store.settings();scope=self._journal.scope()
            if not nickname(cfg['self_name']):raise ValueError('请先建立本机档案')
            recent=time.time()-86400
            records,needed=self._retry_pages(cfg)
            selected_ids={m['id'] for m in records if needed[m['id']]
                          and (m['id']==match_id if match_id else m['created']>=recent)}
            selected={}
            for ident,path,raw,linked,key in self._store.db.execute("""SELECT j.id,j.path,j.payload,r.match_id,r.game_key FROM capture_jobs j
                LEFT JOIN capture_rounds r ON r.id=j.round_id WHERE j.scope=? AND j.created>=?
                AND j.kind IN ('settlement','manual','probe') AND j.status IN ('failed','review','archived')
                AND COALESCE(r.discarded,0)=0 ORDER BY j.created DESC LIMIT 120""",(scope,recent)).fetchall():
                linked=linked or self._retry_linked_match(scope,key)
                if linked not in selected_ids and (linked or match_id):continue
                if not self._retry_useful(needed,linked,json.loads(raw) if raw else {}):continue
                if existing_media_path(self._store.root,path):
                    selected.setdefault(path,('job',ident))
            for record in records:
                if record['id'] not in selected_ids or not record.get('round_id'):continue
                for evidence in record['evidence']:
                    if evidence['kind'] in needed[record['id']] and existing_media_path(self._store.root,evidence['path']):
                        selected.setdefault(evidence['path'],('evidence',(record['id'],evidence['kind'],evidence['path'])))
            queued=0
            for kind,value in list(selected.values())[:24]:
                item=(scope,kind,value)
                if item not in self._requested_retries and len(self._requested_retries)<24:
                    self._requested_retries.append(item);queued+=1
            if queued:
                self._diagnostics.add('retry_saved')
                self._runtime['recognition']={'stage':'queued','page':'settlement','pending_count':queued}
            return {'queued':queued,'state':self.state()}

    def _retry_pages(self, config):
        from matchlog.match_audit import _needed_pages
        from matchlog.completeness import record_status
        from matchlog.rp_reconcile import apply_review
        records=[m for m in self._store.all() if (m['season'],m['split'])==(config['season'],config['split'])]
        own=config['self_name']
        review=self._store.rank_progress(records)['review']
        needed={}
        for match in records:
            ident=match['id']
            current=apply_review(match,review.get(ident))
            needed[ident]=set() if record_status(current,own)['complete'] else _needed_pages(current,own,review)
        return records,needed

    def _retry_linked_match(self, scope, key):
        if not key:return None
        matches=self._store.db.execute("""SELECT DISTINCT match_id FROM capture_rounds
            WHERE scope=? AND game_key=? AND discarded=0 AND match_id IS NOT NULL LIMIT 2""",(scope,key)).fetchall()
        return matches[0][0] if len(matches)==1 else None

    @staticmethod
    def _retry_useful(needed, linked, observation):
        page=observation.get('kind')
        if page in ('postmatch','rank_overview','death_recap','training','hud','lobby','profile','intro'):return False
        if not linked:return True
        pages=needed.get(linked,set())
        return bool(pages) and (page not in ('rank','summary') or page in pages)

    def _run_requested_retry(self,item):
        from matchlog.match_audit import fill_from_image,recover_orphan
        scope,kind,value=item
        with self._store.lock:
            if scope!=self._journal.scope():return
            records,needed=self._retry_pages(self._store.settings())
            if kind=='job':
                job=self._journal.get(value)
                round_row=self._store.db.execute(
                    'SELECT match_id,game_key FROM capture_rounds WHERE id=? AND discarded=0',
                    (job['round_id'],),
                ).fetchone()
                if not round_row:return
                linked=round_row[0] or self._retry_linked_match(scope,round_row[1])
                if not self._retry_useful(needed,linked,job.get('payload') or {}):return
            elif value[1] not in needed.get(value[0],set()):return
        if kind=='job':
            path=existing_media_path(self._store.root,job['path'])
            if not path:return
            self._parsed_frames.pop(job['path'],None)
            if linked and round_row[0]:
                self._run_capture_job(job,repair=True)
            else:
                self._set_recognition('reading',job)
                recovered=recover_orphan(self._store,self._ocr,job)
                self._set_recognition('saved' if recovered else 'review',job)
            return
        ident,page,original=value
        match=next((m for m in records if m['id']==ident),None)
        path=existing_media_path(self._store.root,original)
        if not match or not path or page not in ('summary','rank'):return
        rid=match['round_id'];at=aware_time(match.get(page+'_captured_at') or match['time']).timestamp()
        job={'kind':page,'round_id':rid,'created':at}
        self._set_recognition('reading',job)
        result=self._ocr.read(path)
        saved=result.get('kind')==page and fill_from_image(self._store,result,path,rid,at)
        self._set_recognition('saved' if saved else 'review',job)

    def _set_recognition(self,stage,job,page=None):
        with self._store.lock:
            rid=job.get('round_id')
            row=self._store.db.execute('SELECT started,phase FROM capture_rounds WHERE id=?',(rid,)).fetchone() if rid else None
            start=row[0] if row and row[1] not in ('upload','alias') and row[0]<job['created']-3 else None
            count=self._store.db.execute("""SELECT COUNT(*) FROM capture_jobs WHERE scope=? AND status='pending'
                        AND kind IN ('settlement','manual')""",(self._journal.scope(),)).fetchone()[0]
            self._recognition_updated=time.monotonic()
            self._runtime['recognition']={'stage':stage,'page':page or job.get('kind'),'round_id':rid,'round_started_at':start,
                                         'captured_at':job['created'],'pending_count':count+len(self._requested_retries)}
            if stage=='reading' and job.get('kind') in ('settlement','manual'):
                self._debug('正在识别',game=self._queued_games.get(job.get('id')))

    def _capture_completion(self, match, own_name, page):
        """Publish separate capture milestones after the matching record is committed."""
        from matchlog.completeness import page_complete,record_status
        rank=match.get('rank') or {}
        ranked=match.get('mode')=='ranked'
        summary_saved=match.get('record_source')=='settlement' and page_complete(match,'summary',own_name)
        rp_confirmed=(ranked and page_complete(match,'rank',own_name) and not match.get('rp_manual')
                      and ((rank.get('validation') or {}).get('state')=='match'
                           or rank.get('delta_source') in ('anchors','breakdown')))
        complete=record_status(match,own_name)['complete'] and (not ranked or rp_confirmed)
        completion={'round_id':match.get('round_id'),'match_id':match['id'],'summary_saved':summary_saved,
                    'rp_confirmed':rp_confirmed,'complete':complete,'ranked':ranked}
        previous=self._runtime.get('capture_completion',{})
        self._runtime['capture_completion']=completion
        if completion==previous:return None
        if complete:return Notice('整局已补齐','已保存结算证据','success')
        if page=='summary' and summary_saved:return Notice('总结已留存','可以翻页','success')
        if page=='rank' and rp_confirmed:return Notice('RP 已核验','本局分数已保存','success')
        return None

    def export(self, match_ids=None):
        import webview
        path=self._window.create_file_dialog(webview.FileDialog.SAVE,save_filename='DiveTrail-对局记录.csv',
                                            file_types=('CSV (*.csv)',))
        if not path:
            return {'ok':False}
        if isinstance(path,(list,tuple)):
            path=path[0]
        matches=self.state()['matches']
        if match_ids is not None:
            if not isinstance(match_ids,list) or any(not isinstance(ident,str) for ident in match_ids):
                raise ValueError('导出范围不正确')
            selected=set(match_ids)
            matches=[match for match in matches if match['id'] in selected]
        def safe(value):
            return "'"+value if isinstance(value,str) and value.startswith(('=','+','-','@')) else value
        with open(path,'w',newline='',encoding='utf-8-sig') as f:
            writer=csv.writer(f)
            writer.writerow(['时间','赛季','赛段','模式','地图','排名','本局RP','昵称','使用英雄','击杀','助攻','击倒','伤害','生存时间','救起','重生'])
            for m in reversed(matches):
                summary=m['summary'] or {}
                for p in summary.get('players') or [{}]:
                    writer.writerow([safe(v) for v in [m['time'],m['season'],m['split'],{'ranked':'排位','casual':'匹配','event':'活动'}.get(m.get('mode','ranked'),'待识别'),m.get('map_name'),summary.get('placement'),
                        (m['rank'] or {}).get('delta'),display_name(p.get('name') or ''),p.get('hero'),p.get('kills'),p.get('assists'),p.get('knocks'),
                        p.get('damage'),p.get('survival'),p.get('revives'),p.get('respawns')]])
        return {'ok':True,'count':len(matches)}

    def _process_file(self, path, manual=False, game=None, captured_at=None, round_id=None, job_id=None, recognized=None, fill_only=False):
        scope=self._store.settings()
        self._debug('正在识别截图','读取结算或局内数据',game)
        result=recognized if recognized is not None else self._ocr.read(path)
        kind=result['kind']

        def diagnose(code):
            if job_id:
                self._diagnostics.add(code,phase=kind,job=job_id,round_id=round_id)
                from matchlog.capture_trace import REASONS
                if code in REASONS:self._job_reasons[job_id]=code

        current=self._store.settings()
        if any(scope[k]!=current[k] for k in ('season','split','self_name')):
            diagnose('wrong_scope')
            self._runtime.update(message='赛季／赛段已切换，请重新采集',error=False)
            return {'kind':'unknown','saved':False}
        if round_id and kind in ('summary','profile'):
            from matchlog.profile_rename import relabel_historical_result
            result=relabel_historical_result(self._store,result,round_id,captured_at)
        diagnose('unknown' if kind in ('unknown','other') else 'recognized')
        provisional_round=round_id
        if job_id and kind in ('summary','rank'):
            observation=self._journal.get(job_id)['payload'] or {}
            provisional_round=observation.get('_origin_round_id') or round_id
        observed_at=time.time() if captured_at is None else captured_at
        if round_id and result.get('game_key') and (not manual or job_id) and kind in ('summary','rank'):
            previous_round=round_id
            round_id=self._journal.bind_game_key(round_id,result['game_key'],observed_at,phase='settlement',
                                                job_id=job_id,allow_alias=False,observation=result)
            if not round_id:
                diagnose('record_blocked')
                return {'kind':kind,'saved':False}
            if key:=self._journal.key_for_round(round_id):
                result['game_key']=key
            if round_id!=previous_round:
                diagnose('round_relinked')
        elif kind in ('summary','rank') and not result.get('game_key'):
            diagnose('game_key_missing')
        if job_id and kind=='summary':
            from matchlog.match_audit import with_rank_placement
            result=with_rank_placement(self._store,result,round_id)
        mode=result.get('mode') or ('ranked' if kind=='rank' else None)
        if mode and (not manual or game):self._store.detect_mode(mode,round_id,source='lobby' if kind=='lobby' else 'screen',timestamp=captured_at)
        self._runtime['size']=result['size']
        if kind=='profile':
            if fill_only:return {'kind':kind,'saved':False,'ignored':True}
            if not result.get('name') or nickname(result['name'])!=nickname(current['self_name']):
                diagnose('identity_unconfirmed')
                return {'kind':kind,'saved':False,'ignored':True}
            from matchlog.image_storage import cached_asset,remember_asset,save_archive,timestamp_path
            folder=media_dir(self._store.root,'profiles')
            result={**result,'source':'upload' if manual else 'capture'}
            digest=hashlib.sha256(path.read_bytes()).hexdigest()
            saved=cached_asset(self._store,'profile',digest,folder)
            if saved is None:
                from PIL import Image
                saved=timestamp_path(folder,'profile',captured_at)
                with Image.open(path) as image:save_archive(image,saved)
            changed=self._store.save_profile(result,saved,timestamp=captured_at)
            if changed:
                with self._store.lock,self._store.db:
                    remember_asset(self._store,'profile',digest,saved)
            if not changed and not self._store.db.execute('SELECT 1 FROM profile_snapshots WHERE path=?',(str(saved.resolve()),)).fetchone():saved.unlink(missing_ok=True)
            self._runtime.update(message='个人资料快照已保存',error=False)
            return {'kind':kind,'saved':False,'profile':True}
        if kind=='training':
            self._runtime.update(location='靶场',message='靶场练习中，不计入对局记录',error=False)
            self._debug('练习中','不记录靶场击杀、伤害或排位分',game)
            return {'kind':'training','saved':False,'ignored':True}
        if kind in ('postmatch','death_recap'):
            self._maybe_navigate(result,path,game,round_id,captured_at)
            return {'kind':kind,'saved':False,'ignored':True}
        if kind=='intro':
            if job_id and not manual and self._journal.get(job_id)['kind']=='intro':
                own=nickname(current['self_name'])
                roster=[token for token in result.get('tokens',[]) if .13<token.get('y',0)<.52
                        and token.get('x',1)<.42 and token.get('score',1)>=.88]
                if own and any(nickname(token['text'])==own for token in roster):
                    ident=self._store.open_round(round_id,result.get('entry_cost'),map_name=result.get('map_name'))
                    if ident:
                        self._note_capture(round_id,'start')
                        self._diagnostics.add('open_round',job=job_id,round_id=round_id,phase='intro')
                    return {'kind':kind,'saved':False,'ignored':True}
                diagnose('identity_unconfirmed')
                return {'kind':kind,'saved':False}
            if manual:self._runtime.update(message='加载画面没有战绩，请在结算时按 F8',stage='加载画面',error=False)
            return {'kind':kind,'saved':False,'ignored':True}
        if kind=='rank_overview':
            diagnose('overview_only')
            if job_id:self._journal.observe(job_id,{k:v for k,v in result.items() if k not in ('tokens','size')},json.dumps(result.get('progress')))
            self._maybe_navigate(result,path,game,round_id,captured_at)
            return {'kind':kind,'saved':False,'pending':True,'warnings':result.get('warnings',[])}
        if kind=='lobby':
            from PIL import Image
            saved=self._save_lobby(Image.open(path).convert('RGB'),result,force=True,captured_at=captured_at,round_id=round_id)
            return {'kind':'lobby','saved':False,'lobby':bool(saved or mode)}
        if kind=='unknown':
            self._candidate=None
            self._candidate_count=0
            if manual:
                self._runtime.update(message='未找到结算或局内数据，请导入完整游戏截图',error=True)
            return {'kind':kind,'saved':False}
        clean={k:v for k,v in result.items() if k not in ('tokens','size')}
        sig=json.dumps(clean,sort_keys=True,ensure_ascii=False)
        if kind=='rank' and result.get('tally_pending'):
            diagnose('tally_pending')
            if job_id:self._journal.observe(job_id,clean,sig)
            self._runtime.update(message='结算计分中 · 等待排位明细',error=False)
            return {'kind':kind,'saved':False,'pending':True}
        if not manual and not fill_only and kind!='hud':
            if job_id:
                stable=self._journal.observe(job_id,clean,sig)
                if not stable and kind=='summary' and result.get('game_key') and result.get('tokens'):
                    from matchlog.completeness import summary_complete
                    stable=summary_complete(result,current['self_name'])
            else:
                if sig==self._candidate:self._candidate_count+=1
                else:self._candidate=sig;self._candidate_count=1
                stable=self._candidate_count>=2
            if not stable:
                diagnose('unstable')
                self._runtime.update(message='发现结算画面 · 等待数值稳定',error=False)
                return {'kind':kind,'saved':False,'pending':True}
        if kind=='hud':
            self._candidate=None
            self._candidate_count=0
            self._round_complete=False
            self._store.ingest(result,path)
            from PIL import Image
            backup=self._save_hud_backup(Image.open(path).convert('RGB'),result,force=manual,captured_at=captured_at,round_id=round_id)
            if not backup:
                diagnose('identity_unconfirmed')
            if manual and backup:
                self._runtime.update(message='局内快照已保存',stage='手动补拍完成',error=False)
            return {'kind':kind,'saved':False,'backup':bool(backup)}
        if kind=='rank' and result.get('delta') is None:
            diagnose('missing_rp')
            self._runtime.update(message='排位页尚未读到加减分，等待下一帧',error=False)
            return {'kind':kind,'saved':False}
        # A summary without our own player is not necessarily our match.
        if kind=='summary':
            own=nickname(self._store.settings()['self_name'])
            if not any(nickname(p['name'])==own for p in result.get('players',[])):
                diagnose('identity_unconfirmed')
                self._runtime.update(message='战绩页未找到自己的昵称，请检查设置或用快捷键重试',error=True)
                return {'kind':kind,'saved':False}
        if job_id and kind in ('summary','rank'):
            if not fill_only:
                self._journal.reconcile_settlement(provisional_round,round_id,result,observed_at)
            if round_id:
                round_id=self._journal.settlement_alias(round_id,result,observed_at)
                if not round_id:
                    diagnose('record_blocked')
                    return {'kind':kind,'saved':False}
        from matchlog.image_storage import archive_settlement
        before_revision=self._store.dashboard_revision()
        raw=path.read_bytes()
        digest=hashlib.sha256((round_id or '').encode()+raw).hexdigest()
        saved,new_archive=archive_settlement(self._store,kind,raw,captured_at)
        try:
            match_id=self._store.ingest(result,saved,digest=digest,timestamp=captured_at,round_id=round_id,manual=manual,fill_only=fill_only)
        finally:
            if new_archive and not self._store.db.execute('SELECT 1 FROM evidence WHERE path=? LIMIT 1',(str(saved),)).fetchone():
                saved.unlink(missing_ok=True)
        if match_id:
            if kind=='summary' and not manual:
                from matchlog.match_audit import merge_repeated_snapshot
                if merge_repeated_snapshot(self._store,match_id):diagnose('duplicate')
            if kind=='rank' and not manual:
                from matchlog.match_audit import merge_repeated_rank_page
                unified=merge_repeated_rank_page(self._store,match_id)
                if unified!=match_id:
                    match_id=unified
                    diagnose('duplicate')
            if round_id and round_id not in self._settlement_verify_until:
                self._settlement_verify_until[round_id]=time.monotonic()+12
                if len(self._settlement_verify_until)>32:self._settlement_verify_until.pop(next(iter(self._settlement_verify_until)))
            self._debug('截图已保存','正在核对本局记录是否完整',game)
            match=next((m for m in self._store.all() if m['id']==match_id),None)
            if match:
                round_id=match.get('round_id') or round_id
                self._note_capture(round_id,kind)
                self._sync_highlight(match)
                if not any(b.get('match_id')==match_id for b in self._store.hud_backups()):
                    self._store.link_latest_backup(match_id,timestamp=captured_at,round_id=round_id)
                own_name=self._store.settings()['self_name']
                notice=saved_notice(match,own_name)
                self._round_complete=notice.tone=='success' and match.get('record_source')!='last_snapshot'
                if self._round_complete and round_id:self._completed_rounds.add(round_id)
                milestone=self._capture_completion(match,own_name,kind)
                if milestone:notice=milestone
                self._runtime.update(message=notice.title+' · '+notice.detail,last=time.time(),error=notice.tone in ('warning','error'))
                completion=self._runtime['capture_completion']
                cue_ready=completion['summary_saved'] if kind=='summary' else completion['rp_confirmed']
                if cue_ready and not manual:
                    self._settlement_sound(kind,round_id,game,captured_at)
        self._maybe_navigate(result,path,game,round_id,captured_at)
        if not match_id:
            diagnose('record_blocked')
        from matchlog.completeness import page_complete
        match=next((m for m in self._store.all() if m['id']==match_id),{})
        own_name=self._store.settings()['self_name']
        complete=page_complete(match,kind,own_name)
        unchanged=bool(match_id and self._store.dashboard_revision()==before_revision)
        return {'kind':kind,'saved':bool(match_id),'id':match_id,'round_id':round_id,'page_complete':complete,
                'unchanged':unchanged,'warnings':result.get('warnings',[])}

    def _maybe_navigate(self,result,path,game,round_id,captured_at):
        if not self._auto_switch_enabled or not game or not self._running or not self._store.settings()['auto_switch']:return None
        current=self._journal.current_round()
        if not current or current['id']!=round_id or current['phase']!='settlement':return None
        if captured_at is None or not 0<=time.time()-captured_at<8:return None
        if result.get('kind')=='rank' and time.monotonic()<self._settlement_verify_until.get(round_id,0):return None
        match=next((m for m in self._store.all() if m.get('round_id')==round_id),{})
        from matchlog.completeness import page_complete,summary_complete
        own_name=self._store.settings()['self_name']
        recorded={'rank'} if page_complete(match,'rank',own_name) else set()
        # A readable roster can advance to the RP page to obtain missing placement.
        roster_ready=(result.get('kind')=='summary' and 'rank' not in recorded
                      and summary_complete(match.get('summary'),own_name,require_placement=False))
        if match.get('record_source')!='last_snapshot' and (page_complete(match,'summary',own_name) or roster_ready):
            recorded.add('summary')
        from PIL import Image
        from matchlog.windows_capture import click_page_tab
        reference=path if isinstance(path,Image.Image) else Image.open(path)
        try:target=self._navigator.step(result,recorded,round_id,time.monotonic(),lambda token:click_page_tab(token,game,reference))
        finally:
            if reference is not path:reference.close()
        if target:
            self._runtime['message']={'root-summary':'已切到总结大类','summary':'正在采集三人总结','rank':'正在采集经验值分数'}[target]
        return target

    def _run_capture_job(self, job, confirm=False, repair=False):
        from PIL import Image
        from matchlog.capture_trace import record
        started=time.time()
        ocr_ms=None
        route=None
        input_px=None
        self._job_reasons.pop(job['id'],None)
        def trace(outcome,page=None,reason=None,input_px=None,round_id=None):
            self._write_job_trace(job,record,started,outcome,page,reason,ocr_ms,route,input_px,round_id)
        diagnostic={'job':job['id'],'round_id':job['round_id'],'phase':job['kind']}
        if job['scope']!=self._journal.scope():
            self._diagnostics.add('wrong_scope',**diagnostic)
            self._journal.finish(job['id'],'review','请切回采集时的赛季和账号后补录')
            trace('review',reason='wrong_scope')
            return {'saved':False}
        self._diagnostics.add('reading',**diagnostic)
        self._capture_reads+=1
        self._set_recognition('reading',job)
        source=resolve_media_path(self._store.root,job['path'])
        with Image.open(source) as opened:
            image=opened.convert('RGB')
        kind=job['kind']
        cached=None
        if kind=='probe':kind=self._ocr.screen_hint(image,allow_fallback=True)
        manual=bool(not repair and (confirm or job['manual']))
        asset=frame_asset(image) if kind=='settlement' and not (manual or repair) else None
        if asset is not None and time.monotonic()>=self._settlement_verify_until.get(job['round_id'],0):
            known=self._confirmed_views.get(job['round_id'],{}).get(asset)
            if known is not None:
                self._diagnostics.add('duplicate',**diagnostic)
                self._queued_games.pop(job['id'],None)
                self._journal.finish(job['id'],'done')
                self._set_recognition('idle',job)
                trace('ignored',page=known.get('kind'),reason='duplicate')
                return {'kind':'other','saved':False,'ignored':True}
        if kind=='hud' and not manual:
            payload=job['payload'] or {}
            saved=self._save_hud_backup(image,force=True,captured_at=job['created'],round_id=job['round_id'],repair=repair,
                                        roster_frame=payload.get('_hud_roster',True),
                                        managed_path=source if payload.get('_hud_managed') else None,
                                        tail=bool(payload.get('_hud_tail')) and not repair)
            route='hud'
            outcome={'kind':'hud','saved':False,'backup':bool(saved)}
            if not saved:
                self._diagnostics.add('identity_unconfirmed',**diagnostic)
                self._job_reasons[job['id']]='identity_unconfirmed'
            if saved and len(saved.get('players',[]))==3 and all(p.get('hero') for p in saved['players']):
                sample=self._hud_sampling.get(job['round_id'])
                if sample:sample['confirmed']=True
            if saved and saved.get('round_id')!=job['round_id']:
                with self._store.lock,self._store.db:
                    self._store.db.execute('UPDATE capture_jobs SET round_id=? WHERE id=?',(saved['round_id'],job['id']))
            if saved and '_hud_key' not in (job['payload'] or {}) and not saved.get('game_key') and any(saved.get(k) is not None for k in ('kills','assists','damage')):
                self._journal.archive_earlier_hud(job['round_id'],job['created'])
        elif kind=='lobby' and not manual:
            game=self._queued_games.pop(job['id'],None)
            recent=not repair and self._desktop and self._launched_at<=job['created'] and 0<=time.time()-job['created']<=15
            same_profile=not recent or self._check_lobby_profile(image,job['created'],game)
            if not same_profile or job['scope']!=self._journal.scope():
                outcome={'kind':'lobby','saved':False,'ignored':True}
            else:
                saved=self._save_lobby(image,force=True,captured_at=job['created'],round_id=job['round_id'])
                outcome={'kind':'lobby','saved':False,'lobby':bool(saved)}
                if not saved or saved.get('progress') is None:
                    self._diagnostics.add('lobby_only',**diagnostic)
            route='lobby'
        elif kind in ('settlement','manual','profile','intro') or manual:
            cached=self._parsed_frames.get(job['path'])
            if cached is None:
                at=time.perf_counter()
                read=self._ocr.read
                cached=read(source,image=image) if getattr(read,'__func__',None) is Recognizer.read else read(source)
                ocr_ms=(time.perf_counter()-at)*1000
                route=getattr(self._ocr,'last_read_route',None) or 'full'
                input_px=getattr(self._ocr,'last_read_pixels',None) or image.size
                self._runtime['ocr_size']=input_px
                self._parsed_frames[job['path']]=cached
                if len(self._parsed_frames)>8:self._parsed_frames.pop(next(iter(self._parsed_frames)))
            else:route='cached'
            outcome=self._process_file(Path(job['path']),manual=manual,game=self._queued_games.pop(job['id'],None),
                captured_at=job['created'],round_id=job['round_id'],job_id=job['id'],recognized=json.loads(json.dumps(cached)),fill_only=repair)
        else:outcome={'kind':'other','saved':False,'ignored':True}
        done=any(outcome.get(key) for key in ('backup','lobby','profile','ignored')) or (outcome.get('saved') and outcome.get('page_complete'))
        self._journal.finish(job['id'],'done' if done else 'review',None if done else '单帧或数据未读全，请查看原图核对')
        if kind=='hud' and not manual and saved and payload.get('_hud_managed'):
            self._journal.trim_hud(saved['round_id'])
            if payload.get('_hud_tail') and not repair:self._commit_snapshot(saved,saved['round_id'])
        rid=outcome.get('round_id') or job['round_id']
        if rid and outcome.get('kind') in ('rank','summary') and not self._ocr_should_wait():
            from matchlog.match_audit import recover_observed_summary
            recovered=recover_observed_summary(self._store,round_id=rid)
            if recovered:
                self._journal.resolve_page(rid,'summary')
                match=next(m for m in self._store.all() if m['id']==recovered)
                own_name=self._store.settings()['self_name']
                notice=saved_notice(match,own_name)
                self._round_complete=notice.tone=='success'
                if self._round_complete:self._completed_rounds.add(rid)
                milestone=self._capture_completion(match,own_name,'summary')
                if milestone:notice=milestone
                self._runtime.update(message=notice.title+' · '+notice.detail,last=time.time(),error=notice.tone in ('warning','error'))
                if outcome.get('kind')=='summary':
                    outcome.update(saved=True,id=recovered,page_complete=True)
                    done=True
        if outcome.get('unchanged'):
            event='unchanged'
        elif outcome.get('saved'):
            event='saved' if outcome.get('page_complete') else 'partial'
        elif outcome.get('ignored'):
            event='ignored'
        else:
            event='auxiliary' if done else 'review'
        self._diagnostics.add(event,job=job['id'],round_id=rid,phase=outcome.get('kind'))
        trace(event,page=outcome.get('kind'),reason='complete_already' if outcome.get('unchanged') else None,
              input_px=input_px,round_id=rid)
        if outcome.get('kind') in ('summary','rank') or kind in ('settlement','manual'):
            stage='awaiting_frame' if outcome.get('pending') else 'saved' if outcome.get('saved') else 'review' if not done else 'idle'
            self._set_recognition(stage,{**job,'round_id':outcome.get('round_id') or rid},outcome.get('kind'))
        else:self._set_recognition('idle',job)
        if outcome.get('saved') and outcome.get('page_complete'):
            self._journal.resolve_page(rid,outcome['kind'])
        if outcome.get('saved') and not outcome.get('page_complete'):self._parsed_frames.pop(job['path'],None)
        if asset is not None and cached is not None and ((outcome.get('saved') and outcome.get('page_complete')) or outcome.get('kind') in ('postmatch','death_recap','rank_overview')):
            views=self._confirmed_views.setdefault(rid,{})
            views[asset]=cached
            if len(views)>4:views.pop(next(iter(views)))
            if len(self._confirmed_views)>16:self._confirmed_views.pop(next(iter(self._confirmed_views)))
        return outcome

    def _write_job_trace(self,job,write,started,outcome,page,reason,ocr_ms,route,input_px,round_id):
        reason=reason or self._job_reasons.pop(job['id'],None)
        if outcome=='ignored' and not reason:reason='navigation_only'
        with self._store.lock:
            depth=self._store.db.execute("SELECT COUNT(*) FROM capture_jobs WHERE scope=? AND status IN ('pending','deferred')",
                                         (job['scope'],)).fetchone()[0]
            write(self._store,job,round_id=round_id,page=page,outcome=outcome,reason=reason,
                  started=started,ocr_ms=ocr_ms,input_px=input_px,
                  route=route,queue_depth=depth,scope_mode=self._store.settings()['low_impact'])

    def _recognition_worker(self):
        while not self._stop.wait(.15):
            waiting=self._ocr_should_wait()
            transition=self._last_capture_hint in ('intro','legend_select','loading')
            if waiting and not transition:
                with self._processing:self._read_hud_resume()
                self._stop.wait(.5)
                continue
            with self._processing:
                if waiting:
                    current=self._journal.current_round() or {}
                    rid=current.get('id')
                    job=None
                    if self._transition_reads.get(rid,0)<2:
                        job=self._journal.claim(settlement_only=True,recent_after=time.time()-90)
                    if job:
                        self._transition_reads[rid]=self._transition_reads.get(rid,0)+1
                        if len(self._transition_reads)>20:self._transition_reads.pop(next(iter(self._transition_reads)))
                else:
                    job=self._journal.claim(settlement_only=True,recent_after=time.time()-90)
                if not job and not waiting:self._finalize_snapshots()
                if not job:job=self._journal.claim_hud_tail()
                if not job and self._read_hud_tail():continue
                if not job and not waiting:
                    if self._read_hud_resume():continue
                    job=self._journal.claim_hud_roster()
                    if not job and self._last_capture_hint=='hud' and self._runtime.get('in_match'):
                        from matchlog.capture_sampling import HUD_INTERVALS
                        job=self._journal.claim_hud(self._runtime.get('capture_round_id'),limit=len(HUD_INTERVALS))
                    if not job:job=self._journal.claim()
                if not job:
                    if waiting:continue
                    if self._requested_retries:
                        try:self._run_requested_retry(self._requested_retries.pop(0))
                        except Exception:
                            logging.exception('requested screenshot retry failed')
                            with self._store.lock:
                                if (self._runtime.get('recognition') or {}).get('stage')=='reading':
                                    self._runtime.pop('recognition',None)
                        continue
                    if self._last_capture_hint=='settlement' and time.monotonic()-self._last_capture_seen<12:continue
                    if time.monotonic()-self._last_cleanup>3600:
                        from matchlog.capture_cleanup import clean_images
                        self._last_cleanup=time.monotonic()
                        try:clean_images(self._store)
                        except OSError:logging.exception('screenshot cleanup failed')
                    try:
                        if self._repair_incomplete():continue
                    except Exception:logging.exception('idle recognition retry failed')
                    try:
                        self._audit_step()
                        if self._startup_audit.get('state')=='done':self._maintain_existing_records()
                    except Exception:logging.exception('startup audit failed')
                    continue
                try:self._run_capture_job(job)
                except Exception as error:
                    from matchlog.capture_trace import record
                    self._diagnostics.add('recognition_error',job=job['id'],round_id=job['round_id'],phase=job['kind'])
                    logging.exception('queued capture recognition failed')
                    self._journal.finish(job['id'],'failed',str(error))
                    record(self._store,job,page=job['kind'],outcome='failed',reason='recognition_error',
                           scope_mode=self._store.settings()['low_impact'])
                    self._set_recognition('review',job)
                    self._runtime.update(message='原图已保留，识别失败可在「采集线索」重试',error=True)

    def _read_hud_resume(self):
        candidate=self._hud_tail.claim_probe()
        if candidate is None:return False
        if candidate.scope!=self._journal.scope() or self._runtime.get('profile_change'):return True
        try:
            owner=self._store.settings()['self_name']
            with candidate.canvas() as image:
                roster=self._ocr.read_squad(image,self_name=owner,roster_frame=False)
            if (candidate.scope==self._journal.scope()
                    and any(p.get('slot')==2 and nickname(p['name'])==nickname(owner) for p in roster)
                    and self._hud_tail.resume(candidate)):
                self._hud_sampling.get(candidate.round_id,{}).pop('spectator_until',None)
        except Exception:
            logging.exception('HUD owner recovery failed')
        return True

    def _read_hud_tail(self):
        candidates=self._hud_tail.claim_finished()
        if not candidates:return False
        try:
            for candidate in candidates:
                if candidate.scope!=self._journal.scope() or self._runtime.get('profile_change'):break
                try:
                    self._hud_tail_reads+=1
                    with candidate.canvas() as image:
                        saved=self._save_hud_backup(image,force=True,captured_at=candidate.captured_at,
                                                    round_id=candidate.round_id,roster_frame=False,tail=True)
                    if saved:
                        self._commit_snapshot(saved,saved['round_id'])
                        break
                except Exception:
                    logging.exception('final HUD candidate could not be verified')
        finally:
            self._hud_tail.finish()
        return True

    def _repair_incomplete(self):
        """Give the latest useful evidence one idle recovery slot."""
        if self._ocr_should_wait() or time.monotonic()-self._repair_checked<3:
            return False
        self._repair_checked=time.monotonic()
        from matchlog.match_audit import repair_incomplete
        def run_aux(job):
            self._parsed_frames.pop(job['path'],None)
            return self._run_capture_job(job,confirm=True,repair=True)
        def report(event,job,match_id):
            if job:
                from matchlog.capture_trace import record
                record(self._store,job,page=(job.get('payload') or {}).get('kind') or job['kind'],
                       outcome='review' if event in ('repair_conflict','repair_source_missing') else 'auxiliary',reason=event)
        return repair_incomplete(self._store,self._ocr,run_aux=run_aux,report=report)

    def _audit_step(self):
        if self._startup_audit.get('state')=='done':return
        from matchlog.match_audit import inspect_recent,recover_match,recover_profile_fields,recover_orphan
        if self._audit_queue is None:
            report=inspect_recent(self._store)
            self._audit_queue=[row['id'] for row in report['issues']]
            profile=self._store.latest_profile()
            if profile and (profile.get('cached_fields') or any((profile.get(section) or {}).get(k) is None for section in ('lifetime','season') for k in ('matches','wins','damage','kills','highest_kills'))):self._audit_queue.append('@profile')
            orphaned=self._store.db.execute("""SELECT j.id,j.path FROM capture_jobs j JOIN capture_rounds r ON r.id=j.round_id
                WHERE j.scope=? AND j.created>=? AND j.kind IN ('settlement','manual','probe') AND j.status IN ('review','failed')
                  AND r.match_id IS NULL AND r.discarded=0 GROUP BY j.path ORDER BY j.created DESC LIMIT 32""",(self._journal.scope(),time.time()-86400)).fetchall()
            self._audit_queue.extend('@job:'+ident for ident,path in orphaned if existing_media_path(self._store.root,path))
            self._startup_audit={**report,'state':'checking'}
            if self._audit_queue:
                import sqlite3
                folder=self._store.root/'backups';folder.mkdir(exist_ok=True)
                path=folder/('audit-'+datetime.now(timezone(timedelta(hours=8))).strftime('%y%m%d')+'.sqlite3')
                if not path.exists():
                    with self._store.lock:
                        backup=sqlite3.connect(path);self._store.db.backup(backup);backup.close()
        if self._audit_queue:
            ident=self._audit_queue.pop(0)
            try:
                if ident=='@profile':recover_profile_fields(self._store,self._ocr)
                elif ident.startswith('@job:'):recover_orphan(self._store,self._ocr,self._journal.get(ident[5:]))
                else:recover_match(self._store,self._ocr,ident)
            except Exception:logging.exception('recent match recovery failed')
        if not self._audit_queue:
            self._startup_audit={**inspect_recent(self._store),'state':'done'}

    def _finalize_snapshots(self):
        if time.monotonic()-self._last_fallback_check<2:return
        self._last_fallback_check=time.monotonic()
        cfg=self._store.settings()
        with self._store.lock:
            since=self._store.db.execute("SELECT value FROM recorder_flags WHERE name='snapshot_fallback_since'").fetchone()[0]
            rows=self._store.db.execute("""SELECT h.payload,r.id,r.phase FROM hud_backups h
                JOIN capture_rounds r ON r.id=json_extract(h.payload,'$.round_id')
                LEFT JOIN matches m ON m.id=r.match_id
                WHERE h.season=? AND h.split=? AND h.created>=? AND r.scope=? AND r.discarded=0
                AND r.phase!='alias' AND (NOT COALESCE(json_extract(m.payload,'$.corrected'),0)
                                           OR json_extract(m.payload,'$.manual_fields') IS NOT NULL)
                AND (json_extract(m.payload,'$.summary') IS NULL OR
                    (json_extract(m.payload,'$.record_source') IN ('last_snapshot','opening')
                     AND COALESCE(json_extract(m.payload,'$._snapshot_created'),0)<h.created))
                ORDER BY (json_extract(h.payload,'$.kills') IS NOT NULL OR
                          json_extract(h.payload,'$.assists') IS NOT NULL OR
                          json_extract(h.payload,'$.damage') IS NOT NULL) DESC,h.created DESC LIMIT 60""",
                (cfg['season'],cfg['split'],float(since),self._journal.scope())).fetchall()
            seen=set()
            for raw,rid,phase in rows:
                backup=json.loads(raw)
                if not any(backup.get(k) is not None for k in ('kills','assists','damage')) and backup.get('identity')!='self':continue
                if not rid or rid in seen:continue
                seen.add(rid)
                if self._hud_tail.pending(self._journal.scope(),rid):continue
                # Menu/animation flashes and range-template guesses are not match boundaries.
                next_game=self._store.db.execute("""SELECT 1 FROM capture_rounds WHERE scope=? AND started>? AND id!=?
                    AND (game_key IS NOT NULL OR phase='intro' OR EXISTS (
                    SELECT 1 FROM matches WHERE id=match_id AND json_extract(payload,'$.round_started_at') IS NOT NULL))
                    AND phase NOT IN ('upload','alias') AND discarded=0 LIMIT 1""",
                    (self._journal.scope(),datetime.fromisoformat(backup['time']).timestamp(),rid)).fetchone()
                ended=phase in ('lobby','profile') or bool(next_game)
                if not ended:continue
                self._commit_snapshot(backup,rid)

    def _commit_snapshot(self, backup, rid):
        ident=self._store.finalize_snapshot(backup)
        key=(ident,backup['time'])
        if ident and key not in self._snapshot_notices:
            self._diagnostics.add('fallback',phase='hud',round_id=rid)
            self._snapshot_notices.add(key)
            self._runtime.update(message='最后快照已记录',error=False)

    def _ocr_should_wait(self):
        if not self._store.settings()['low_impact']:return False
        from matchlog.windows_capture import foreground_game
        return bool(foreground_game()) and self._last_capture_hint not in ('lobby','settlement','postmatch','profile')

    def _worker(self):
        from matchlog.windows_capture import (capture_frame,capture_scout,capture_lobby_probe,capture_backup_regions,
                                              capture_hud_candidate,capture_selection_probe,foreground_game)
        from matchlog.capture_sampling import (HUD_INTERVALS,NAVIGATION_INTERVALS,SETTLEMENT_INTERVALS,
                                               due,record_sample,settlement_page)
        while not self._stop.wait(.2):
            if not self._running and not self._requested.is_set() and self._profile_scan is None:
                continue
            if not nickname(self._store.settings()['self_name']):
                self._diagnostics.add('no_profile',every=60)
                self._requested.clear()
                self._runtime.update(message='请先建立本机档案',stage='等待设置档案',error=False)
                if self._profile_scan is not None:
                    self._scan_profile_step()
                continue
            if self._profile_scan is not None:self.cancel_profile_scan()
            self._sync_calendar()
            current_game=foreground_game()
            manual=self._take_manual_capture(current_game)
            if self._runtime.get('profile_change'):manual=False
            if not current_game:
                self._diagnostics.add('no_game',every=60)
                self._runtime.update(in_match=False,game_phase='no_game',capture_round_id=None)
                self._last_pre_match=None
                if self._debugger:self._debugger.suspend(not self._running)
                if self._running:
                    self._runtime.update(message='等待 Apex 位于前台',error=False)
                    self._debug('等待 Apex 前台','目前未采集其他窗口')
                continue
            if not self._running and not manual:continue
            self._runtime['game_size']=(current_game['width'],current_game['height'])
            try:
                top,bottom,game=capture_scout(include_bottom=False)
                self._scout_count+=1
                if game!=current_game:raise RuntimeError('游戏窗口已切换，已跳过本次采集')
                # If text scouting is needed, skip the cycle while OCR is busy.
                hint=self._ocr.screen_hint_regions(top,bottom,(game['width'],game['height']))
                if hint is None:
                    if not manual:
                        self._requested.wait(.8)
                        continue
                    hint='other'
                now=time.monotonic()
                if hint=='training':
                    self._training_seen=now
                elif hint in ('hud','other','spectator') and self._training_seen is not None and now-self._training_seen<12:
                    hint='training'
                else:
                    self._training_seen=None
                if hint=='lobby_candidate':
                    bottom=capture_lobby_probe(game)
                    hint=self._ocr.screen_hint_regions(top,bottom,(game['width'],game['height'])) or 'other'
                if hint=='lobby' and bottom is None:
                    bottom=capture_lobby_probe(game)
                cfg=self._store.settings()
                auto_switch=self._auto_switch_enabled and cfg['auto_switch']
                selection_due=(self._last_selection_probe is None or now-self._last_selection_probe>=4
                               or self._last_capture_hint=='legend_select')
                if hint=='other' and not manual and selection_due and self._last_capture_hint in (None,'lobby','settlement','postmatch','intro','loading','legend_select'):
                    self._last_selection_probe=now
                    if self._ocr.is_legend_selection(capture_selection_probe(game),game['width']):hint='legend_select'
                if hint in ('legend_select','intro'):self._last_pre_match=now
                elif hint!='other':self._last_pre_match=None
                if hint=='other' and self._last_pre_match is not None and now-self._last_pre_match<=10:hint='loading'
                previous_hint=self._last_capture_hint
                self._last_capture_hint=hint
                self._last_capture_seen=now
                if self._runtime.get('profile_change') and hint!='lobby':
                    self._requested.wait(1.8)
                    continue
                self._diagnostics.add('seen',phase=hint,every=None if hint!=previous_hint else 60)
                low_impact=cfg['low_impact']
                self._runtime.update(in_match=hint not in ('lobby','settlement','postmatch','profile'),
                                     game_phase=hint)
                if self._debugger:self._debugger.suspend(not self._running or (low_impact and self._runtime['in_match'] and hint not in ('legend_select','intro','loading')))
                mode_name={'ranked':'排位','casual':'匹配','event':'活动'}.get(self._store.current_mode(),'模式待识别')
                locations={'lobby':'大厅','profile':'个人资料','training':'靶场','spectator':'观战','postmatch':'赛后界面'}
                self._runtime['location']=locations.get(hint,mode_name if hint in ('hud','settlement','intro','legend_select','loading') else '待识别')
                at=time.time()
                journal_hint=hint
                if hint in ('legend_select','loading','spectator'):
                    journal_hint='other'
                elif hint=='postmatch':
                    journal_hint='settlement' if auto_switch else 'other'
                with self._store.lock:
                    current=self._store.settings()
                    if any(current[key]!=cfg[key] for key in ('self_name','season','split')):continue
                    scope=self._journal.scope()
                    round_id=self._journal.note_screen(journal_hint,at)
                    previous_round=self._hud_tail.round_id(scope)
                    if (hint=='hud' and previous_round and previous_round!=round_id
                            and self._journal.canonical_round(previous_round)==round_id):
                        self._hud_tail.rebind(scope,previous_round,round_id)
                    if self._running:self._hud_tail.screen(scope,round_id,hint)
                self._runtime['capture_round_id']=round_id
                kind=None
                hud_key=None
                roster_frame=True
                sample=None
                candidate=None
                if hint=='training':
                    self._runtime.update(message='靶场练习中，不计入对局记录',error=False)
                    self._debug('练习中','不记录靶场数据',game)
                elif hint in ('legend_select','intro','loading') and not manual:
                    stage={'legend_select':'英雄选择','intro':'排位倒计时','loading':'等待进入对局'}[hint]
                    self._runtime.update(message=stage,error=False)
                    self._debug(stage,game=game)
                    if hint=='intro' and previous_hint=='intro' and round_id and round_id not in self._queued_openings:
                        kind='intro'
                elif manual:
                    kind='manual'
                    if not round_id:round_id=self._journal.note_screen('manual',at)
                    self._runtime['capture_round_id']=round_id
                elif hint in ('settlement','postmatch'):
                    if round_id and round_id not in self._completed_rounds and (hint=='settlement' or auto_switch):
                        pages=self._settlement_sampling.setdefault(round_id,{})
                        if len(self._settlement_sampling)>32:self._settlement_sampling.pop(next(iter(self._settlement_sampling)))
                        page=settlement_page(top) if hint=='settlement' else 'navigation'
                        sample=pages.setdefault(page,{})
                        intervals=SETTLEMENT_INTERVALS if hint=='settlement' else NAVIGATION_INTERVALS
                        if due(sample,now,intervals):kind='settlement'
                    if previous_hint not in ('settlement','postmatch'):
                        self._settlement_sound('end',round_id,game)
                    self._debug('结算' if hint=='settlement' else '赛后界面',game=game)
                elif hint=='spectator':
                    self._debug('观战',game=game)
                elif hint=='hud':
                    sample=self._hud_sampling.setdefault(round_id,{'confirmed':False,'rosters':0})
                    if len(self._hud_sampling)>32:self._hud_sampling.pop(next(iter(self._hud_sampling)))
                    remembered=False
                    if self._hud_tail.wants_frame(scope,round_id,now):
                        candidate=capture_hud_candidate(game,top,scope,round_id,at)
                        self._hud_candidate_count+=1
                        remembered=self._hud_tail.remember(candidate,now)
                    if now<sample.get('spectator_until',0):
                        self._runtime.update(location='观战',game_phase='spectator')
                        self._debug('观战',game=game)
                    else:
                        if remembered and due(sample,now,HUD_INTERVALS):
                            if sample.get('count',0)<2 or sample.get('key')!=candidate.fingerprint:
                                kind='hud'
                                hud_key=candidate.fingerprint
                                roster_frame=not sample['confirmed'] and sample['rosters']<2
                        self._debug('对局进行中',game=game)
                elif hint=='lobby':
                    interval=2 if self._lobby_name_sighting else 10 if self._runtime.get('profile_change') else 120
                    if previous_hint!='lobby' or self._last_queued_lobby is None or now-self._last_queued_lobby>=interval:kind='lobby'
                    self._debug('大厅',game=game)
                elif hint=='profile':
                    if previous_hint!='profile' or self._last_profile is None or now-self._last_profile>=120:
                        kind='profile';self._last_profile=now
                    self._debug('资料页检测中',game=game)
                elif hint=='other':
                    self._debug('画面待确认',game=game)
                if kind:
                    if kind=='hud':
                        image=capture_backup_regions(kind,game,top,bottom) if roster_frame else candidate.canvas()
                    elif kind=='lobby':image=capture_backup_regions(kind,game,top,bottom)
                    else:
                        image,full_game=capture_frame()
                        if full_game!=game:raise RuntimeError('游戏窗口已切换，已跳过本次采集')
                    if sample is not None and kind!='hud':record_sample(sample,now)
                    queued_image=queued_frame(image,kind)
                    if kind=='settlement' and now>=self._settlement_verify_until.get(round_id,0):
                        views=self._confirmed_views.get(round_id)
                        known=views.get(frame_asset(queued_image)) if views else None
                        if known is not None:
                            self._diagnostics.add('duplicate',phase=hint,round_id=round_id,every=15)
                            if auto_switch:
                                self._maybe_navigate(known,image,game,round_id,at)
                            self._requested.wait(.8)
                            continue
                    with self._store.lock:
                        current=self._store.settings()
                        if (any(current[key]!=cfg[key] for key in ('self_name','season','split'))
                                or self._runtime.get('profile_change') and kind!='lobby'):
                            continue
                        client_px=(game['width'],game['height'])
                        if kind=='hud':
                            ident=self._journal.enqueue(image,kind,round_id,manual,timestamp=at,
                                                        hud_key=hud_key,hud_roster=roster_frame,client_px=client_px,
                                                        managed_hud=True)
                        else:
                            ident=self._journal.enqueue(queued_image,kind,round_id,manual,timestamp=at,client_px=client_px)
                    if ident:
                        self._diagnostics.add('deferred' if low_impact and self._runtime['in_match'] else 'queued',
                                              phase=kind,job=ident,round_id=round_id)
                        if kind in ('settlement','manual','probe','lobby'):
                            self._queued_games[ident]=game
                            if len(self._queued_games)>32:self._queued_games.pop(next(iter(self._queued_games)))
                        if kind=='hud':
                            record_sample(sample,now)
                            self._last_queued_hud=(round_id,now)
                            sample.update(key=hud_key,rosters=sample['rosters']+int(roster_frame))
                            self._note_capture(round_id,'hud')
                        if kind=='lobby':self._last_queued_lobby=now
                        if kind=='intro':
                            if len(self._queued_openings)>=64:self._queued_openings.clear()
                            self._queued_openings.add(round_id)
                        if kind in ('settlement','manual') and (self._runtime.get('recognition') or {}).get('stage')!='reading':
                            self._set_recognition('queued',{'kind':kind,'round_id':round_id,'created':at})
                        self._debug('截图已保存','结算／大厅再识别' if low_impact and self._runtime['in_match'] else '后台识别中；快速重开也会保留原图',game)
                    else:
                        self._diagnostics.add('queue_skipped',phase=kind,round_id=round_id,every=15)
                    del image,queued_image
            except Exception as e:
                code='black_frame' if '黑屏' in str(e) else 'focus_lost' if '窗口已切换' in str(e) else 'capture_error'
                if (code=='black_frame' and not manual and self._last_pre_match is not None
                        and time.monotonic()-self._last_pre_match<=10):
                    self._diagnostics.add('seen',phase='loading',every=5)
                    self._runtime.update(message='等待进入对局',error=False)
                    self._debug('等待进入对局',game=current_game)
                    self._requested.wait(.8)
                    continue
                self._diagnostics.add(code,every=15)
                self._runtime.update(message=str(e),error=True)
                self._debug('采集暂未成功',str(e),locals().get('game'))
                last=self._last_capture_error
                if not last or last[0]!=str(e) or time.monotonic()-last[1]>=30:
                    logging.exception('capture/recognition failed')
                    self._last_capture_error=(str(e),time.monotonic())
            self._requested.wait(.8 if self._last_capture_hint=='settlement' else 1.8)

    def _refresh_icons(self):
        from matchlog.windows_startup import set_window_icon
        self._icons=getattr(self,'_icons',[])+set_window_icon(self._title,ROOT/'ui/assets/brand/divetrail.ico')
        if not getattr(self,'_frame',None):
            from matchlog.window_frame import install_frame
            self._frame=install_frame(self._title)

    def _start(self):
        from matchlog.windows_capture import hotkey_loop
        from matchlog.windows_startup import set_window_icon
        from matchlog.notifications import NativeNotifications
        self._refresh_icons()
        from matchlog.media_migration import migrate_media_layout
        try:
            migrate_media_layout(self._store)
        except (OSError,ValueError):
            logging.exception('could not complete local screenshot migration')
        try:self._store.repair_hud_start_times()
        except (OSError,ValueError):logging.exception('could not recover local match start times')
        self._profile_notifier=NativeNotifications(mode='guide')
        self._profile_notifier.start()
        self._debugger=NativeNotifications(mode='debug')
        self._debugger.configure(self._store.settings());self._debugger.start()
        self._debugger.suspend(not self._running)
        self._journal.recover_interrupted()
        self._journal.requeue_detector_errors()
        self._recognition_thread=threading.Thread(target=self._recognition_worker,daemon=True,name='recognition')
        self._capture_thread=threading.Thread(target=self._worker,daemon=True,name='capture')
        self._recognition_thread.start();self._capture_thread.start()
        threading.Thread(target=hotkey_loop,args=(self._hotkey_request,self._stop,
                         self._report_hotkey),daemon=True,name='hotkey').start()

    def _maintain_existing_records(self):
        """Do historical maintenance once per profile and maintenance revision."""
        key=f'archive_maintenance:0.15.0:{self._journal.scope()}'
        if getattr(self,'_archive_maintenance_key',None)==key:return
        with self._store.lock:
            if not self._store.db.execute('SELECT 1 FROM recorder_flags WHERE name=?',(key,)).fetchone():
                self._store.repair_heroes()
                for match in self._store.all():self._sync_highlight(match)
                with self._store.db:
                    self._store.db.execute('INSERT OR REPLACE INTO recorder_flags VALUES (?,?)',(key,int(time.time())))
            self._archive_maintenance_key=key

    def _report_hotkey(self, message):
        self._runtime.update(hotkey_error=message)
        self._diagnostics.add('hotkey_unavailable' if message else 'hotkey_available')

    def _shutdown(self):
        self._stop.set()
        self._requested.set()
        self._running=False
        for name in ('_capture_thread','_recognition_thread'):
            worker=getattr(self,name,None)
            if worker:worker.join(timeout=4)
        if self._profile_notifier:self._profile_notifier.close()
        if self._debugger:self._debugger.close()
        from matchlog.windows_startup import release_icons
        release_icons(getattr(self,'_icons',[]))


def main(instance_name=None):
    import sys
    from matchlog.windows_startup import (acquire_instance,clear_window_taskbar,release_instance,
                                          set_app_identity,set_window_taskbar)
    from matchlog.window_frame import MIN_WINDOW_HEIGHT, MIN_WINDOW_WIDTH
    portable='--portable' in sys.argv
    installed='--installed' in sys.argv and not portable
    data=ROOT/'portable-data' if portable else None
    if portable and data.resolve()!=data:
        raise OSError('便携版的数据目录不能重定向到其他位置')
    if portable:
        key=hashlib.sha256(str(ROOT).casefold().encode()).hexdigest()[:12]
        title=f'{APP_NAME} · 便携版 · {ROOT.name} [{key[:6]}]'
        set_app_identity('DiveTrail.Portable.'+key)
        instance=acquire_instance(instance_name or f'Local\\MatchLogPortable-{key}', title)
    else:
        set_app_identity()
        instance=acquire_instance(instance_name) if instance_name else acquire_instance()
    if instance is None:return
    if installed:
        try:
            from matchlog.windows_install import installed_data_path
            data=installed_data_path()
        except BaseException:
            release_instance(instance)
            raise
    import webview
    from matchlog.windows_capture import dpi_aware
    dpi_aware()
    # Lower only this tool's CPU scheduling priority; never alter the game's process.
    import ctypes as C
    k=C.WinDLL('kernel32');k.GetCurrentProcess.restype=C.c_void_p
    k.SetPriorityClass.argtypes=[C.c_void_p,C.c_uint]
    k.SetPriorityClass(k.GetCurrentProcess(),0x4000)
    api=Api(data) if data is not None else Api()
    if portable:
        api._portable=True
        api._title=title
    startup='--startup' in sys.argv and not portable
    if '--record' in sys.argv or (startup and api._store.settings()['start_on_login']):api._running=True
    if api._running:api._journal.set_running(True)
    log=api._store.root/'logs'/'app.log'
    log.parent.mkdir(parents=True,exist_ok=True)
    from logging.handlers import RotatingFileHandler
    logging.basicConfig(handlers=[RotatingFileHandler(log,maxBytes=2*1024*1024,backupCount=3,encoding='utf-8')],
                        level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s',force=True)
    logging.info('Desktop started; compact layout and configurable settlement sounds')
    api._window=webview.create_window(api._title,str(ROOT/'ui'/'index.html'),js_api=api,
                width=1120,height=740,min_size=(MIN_WINDOW_WIDTH,MIN_WINDOW_HEIGHT),resizable=True,minimized=startup,
                background_color='#0c1117',frameless=True,easy_drag=False,shadow=True)
    api._window.events.closed+=api._shutdown
    def configure_desktop():
        set_window_taskbar(api._window.native.Handle.ToInt64(),ROOT,portable,api._title,
                           ROOT/'ui/assets/brand/divetrail.ico',installed=installed)
        try:
            from matchlog.windows_tray import WindowsTray
            mascot=ROOT/'ui/assets/brand/nessie-mascot.ico'
            if not mascot.is_file():mascot=ROOT/'ui/assets/brand/divetrail-mascot.ico'
            api._tray=WindowsTray(api._window.native,mascot if mascot.is_file() else ROOT/'ui/assets/brand/divetrail.ico',
                                  lambda:api._running,api._set_recording)
        except Exception:
            logging.exception('Could not create the Windows notification area icon')
    def release_desktop():
        try:
            if tray:=getattr(api,'_tray',None):
                api._tray=None
                tray.close()
        finally:
            clear_window_taskbar(api._window.native.Handle.ToInt64())
    api._window.events.before_show+=configure_desktop
    api._window.events.closing+=release_desktop
    api._window.events.shown+=api._refresh_icons
    api._window.events.maximized+=lambda:setattr(api,'_maximized',True)
    api._window.events.restored+=lambda:setattr(api,'_maximized',False)
    try:webview.start(api._start,gui='edgechromium',storage_path=str(api._store.root/'webview'),icon=str(ROOT/'ui/assets/brand/divetrail.ico'))
    finally:release_instance(instance)


if __name__=='__main__':
    main()
