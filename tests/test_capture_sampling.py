import tempfile
import unittest
from datetime import datetime
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

from matchlog.capture_sampling import HUD_INTERVALS, SETTLEMENT_INTERVALS, HudCandidate, HudTail, due, record_sample, settlement_page
from support import Api, Store


class SamplingTests(unittest.TestCase):
    def test_spectating_freezes_until_a_same_round_identity_probe_is_accepted(self):
        tail=HudTail()
        tail.screen('account','round','hud')
        old=HudCandidate('account','round',100,b'digits',(32,32),())
        latest=HudCandidate('account','round',101,b'digits',(32,32),())
        tail.remember(old,100)
        tail.remember(latest,101)
        tail.screen('account','round','spectator')
        watched=HudCandidate('account','round',102,b'spectator',(32,32),())
        self.assertFalse(tail.remember(watched,102))
        self.assertIs(tail.claim_probe(),watched)
        tail.screen('account','round','spectator')
        self.assertFalse(tail.resume(watched))
        tail.screen('account','round','hud')
        respawn=HudCandidate('account','round',113,b'respawn',(32,32),())
        self.assertFalse(tail.remember(respawn,113))
        self.assertIs(tail.claim_probe(),respawn)
        self.assertTrue(tail.resume(respawn))
        tail.screen('account','next','intro')
        tail.screen('account','next','lobby')
        self.assertEqual(tail.claim_finished(),(respawn,latest))
        tail.finish()
        self.assertFalse(tail.claim_finished())

    def test_fast_requeue_reads_the_latest_two_candidates_into_the_old_round(self):
        with tempfile.TemporaryDirectory() as folder:
            api=Api(folder,desktop=False)
            journal=api._journal
            scope=journal.scope()
            old=journal.note_screen('hud',100)
            api._hud_tail.screen(scope,old,'hud')
            for at in (101,190,199):
                api._hud_tail.remember(HudCandidate(scope,old,at,bytes([at]),(32,32),()),at)
            new=journal.note_screen('intro',201)
            self.assertNotEqual(old,new)
            api._hud_tail.screen(scope,new,'intro')
            api._hud_tail.screen(scope,new,'settlement')
            api._ocr.read_hud=Mock(side_effect=[{'damage':999},{'damage':210,'live_rp':17}])
            api._ocr.read_squad=Mock(side_effect=[[{'name':'Spectated','slot':2}],[{'name':'PlayerOne','slot':2}]])
            self.assertTrue(api._read_hud_tail())
            self.assertFalse(api._read_hud_tail())
            match,=api._store.all()
            self.assertEqual((match['round_id'],match['summary']['players'][0]['damage']),(old,210))
            self.assertEqual(datetime.fromisoformat(match['snapshot_at']).timestamp(),190)
            self.assertEqual(api._ocr.read_hud.call_count,2)
            backup,=api._store.hud_backups()
            self.assertTrue(api._store.backup_image(backup['id']).is_file())
            self.assertEqual(api._store.db.execute('SELECT COUNT(*) FROM capture_jobs').fetchone()[0],1)
            api._store.db.close()

    def test_three_shared_hud_files_survive_restart_without_crowding_out_roster_or_tail(self):
        with tempfile.TemporaryDirectory() as folder:
            api=Api(folder,desktop=False)
            rid=api._journal.note_screen('hud',100)
            api._ocr.read_hud=Mock(return_value={'damage':0})
            api._ocr.read_squad=Mock(return_value=[{'name':'PlayerOne','slot':2}])
            with Image.new('RGB',(32,32),'blue') as image:
                opening=api._journal.enqueue(image,'hud',rid,timestamp=101,managed_hud=True)
                api._run_capture_job(api._journal.get(opening))
                for at in range(102,111):
                    image.putpixel((0,0),(at,0,0))
                    recent=api._journal.enqueue(image,'hud',rid,timestamp=at,managed_hud=True,hud_roster=False)
                    self.assertTrue(recent)
                    api._run_capture_job(api._journal.get(recent))
                latest=api._journal.enqueue(image,'hud',rid,timestamp=115,managed_hud=True,hud_roster=False,hud_tail=True)
                orphan=api._store.root/'capture'/'hud'/'hud-frame-700101-080001.webp'
                image.save(orphan)
                api._store.db.close()
                restarted=Api(folder,desktop=False)
                restarted._journal.recover_interrupted()
                self.assertFalse(orphan.exists())
                self.assertEqual(restarted._journal.claim_hud_tail()['id'],latest)
                restarted._ocr.read_hud=Mock(return_value={'damage':50})
                restarted._ocr.read_squad=Mock(return_value=[{'name':'PlayerOne','slot':2}])
                restarted._run_capture_job(restarted._journal.get(latest))
                retained={str(path) for path in (restarted._store.root/'capture'/'hud').glob('*.webp')}
                self.assertEqual(len(retained),3)
                self.assertIn(restarted._journal.get(opening)['path'],retained)
                self.assertIn(restarted._journal.get(latest)['path'],retained)
                self.assertFalse(list((restarted._store.root/'capture'/'draft').glob('*')))
                self.assertEqual(restarted._store.all()[0]['summary']['players'][0]['damage'],50)
                restarted._store.db.close()

    def test_capture_milestones_require_complete_own_summary_and_verified_rp(self):
        with tempfile.TemporaryDirectory() as folder:
            api=Api(folder,desktop=False)
            player={'name':'PlayerOne','kills':0,'assists':0,'knocks':0,'damage':200}
            summary={'placement':5,'expected_players':1,'players':[player]}
            match={'id':'match','mode':'ranked','round_id':'round','record_source':'last_snapshot',
                   'summary':summary,'rank':{'delta':17,'validation':{'state':'unavailable'}}}
            self.assertIsNone(api._capture_completion(match,'PlayerOne','summary'))
            self.assertFalse(api._runtime['capture_completion']['summary_saved'])
            match['record_source']='settlement'
            self.assertEqual(api._capture_completion(match,'PlayerOne','summary').title,'总结已留存')
            self.assertFalse(api._runtime['capture_completion']['rp_confirmed'])
            match['rank']['validation']['state']='match'
            self.assertEqual(api._capture_completion(match,'PlayerOne','rank').title,'整局已补齐')
            self.assertTrue(api._runtime['capture_completion']['rp_confirmed'])
            api._store.db.close()

    def test_static_settlement_tab_exhausts_its_budget_and_tab_switch_is_visible(self):
        with Image.new('RGB',(1920,180),'black') as first, Image.new('RGB',(1920,180),'black') as second:
            ImageDraw.Draw(first).rectangle((760,125,950,134),fill=(237,174,139))
            ImageDraw.Draw(second).rectangle((950,125,1140,134),fill=(237,174,139))
            self.assertNotEqual(settlement_page(first),settlement_page(second))
        state={}
        for second in range(300):
            if due(state,second,SETTLEMENT_INTERVALS):record_sample(state,second)
        self.assertEqual(state['count'],7)
        hud={}
        times=[]
        for second in range(1800):
            if due(hud,second,HUD_INTERVALS):
                record_sample(hud,second)
                times.append(second)
        self.assertEqual(len(times),8)
        self.assertGreater(times[-1],20*60)

    def test_range_f8_and_observer_never_grab_full_images(self):
        with tempfile.TemporaryDirectory() as folder:
            api=Api(folder,desktop=False)
            api._running=True
            api._stop=Mock(wait=Mock(side_effect=[False,False,False,True]))
            api._requested=Mock(wait=Mock(return_value=False))
            api._take_manual_capture=Mock(side_effect=[True,False,False])
            api._ocr.screen_hint_regions=Mock(side_effect=['training','spectator','postmatch'])
            game={'hwnd':1,'left':0,'top':0,'width':1920,'height':1200}
            with Image.new('RGB',(1920,180),'black') as top, \
                 patch('matchlog.windows_capture.foreground_game',return_value=game), \
                 patch('matchlog.windows_capture.capture_scout',return_value=(top,None,game)), \
                 patch('matchlog.windows_capture.capture_frame') as full, \
                 patch('matchlog.windows_capture.capture_backup_regions') as compact:
                api._worker()
            full.assert_not_called()
            compact.assert_not_called()
            self.assertEqual(api._store.db.execute('SELECT count(*) FROM capture_jobs').fetchone()[0],0)
            api._store.db.close()

    def test_earliest_own_hud_fills_start_preserving_confirmed_opening(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(folder)
            rid=store.journal.note_screen('hud',100)
            own={'round_id':rid,'identity':'self','players':[{'name':'PlayerOne','slot':2}],'damage':0}
            store.save_hud_backup(own,'second.webp',timestamp=113)
            store.save_hud_backup(own,'first.webp',timestamp=100)
            store.save_hud_backup({**own,'identity':'spectator'},'watched.webp',timestamp=90)
            store.journal.note_screen('settlement',200)
            store.ingest({'kind':'rank','delta':0},'rank.webp',digest='rank',timestamp=200,round_id=rid)
            match=store.all()[0]
            self.assertEqual((datetime.fromisoformat(match['round_started_at']).timestamp(),match['round_start_source']),(100,'hud'))
            self.assertEqual(datetime.fromisoformat(match['settled_at']).timestamp(),200)
            intro=store.journal.note_screen('intro',300)
            confirmed=store.open_round(intro)
            store.save_hud_backup({**own,'round_id':intro},'later.webp',timestamp=320)
            store.journal.note_screen('settlement',380)
            store.ingest({'kind':'rank','delta':0},'rank.webp',digest='next',timestamp=380,round_id=intro)
            match=next(m for m in store.all() if m['id']==confirmed)
            self.assertEqual((datetime.fromisoformat(match['round_started_at']).timestamp(),match['round_start_source']),(300,'intro'))
            store.db.close()
