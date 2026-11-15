"""First-run OCR stays scoped to an Apex lobby and never commits without confirmation."""

from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from app import Api
from matchlog.recognition import Recognizer, lobby_identity_name, parse_lobby


RANK = parse_lobby('青铜 I\n116/500 RP\n排位赛')
GAME = {'hwnd': 41, 'width': 1920, 'height': 1200}


def token(text, x=0.1, y=0.2, score=0.97):
    return {'text': text, 'x': x, 'y': y, 'bottom': y+0.3, 'score': score}


class LobbyIdentityRecognition(unittest.TestCase):
    def test_name_needs_one_confident_line_and_removes_clan_prefix(self):
        self.assertEqual(lobby_identity_name([token('试验玩家', x=.3), token('[TEST]',score=.75)]), '试验玩家')
        for tokens in ([token('[TEST]')], [token('17')], [token('Player', score=.7)],
                       [token('First'), token('Second', x=.3, y=.6)]):
            with self.subTest(tokens=tokens):
                self.assertIsNone(lobby_identity_name(tokens))

    def test_rank_and_lobby_gate_the_local_name_crop(self):
        ocr = Recognizer()
        ocr.screen_hint = Mock(return_value='other')
        ocr.read_lobby = Mock(return_value=RANK)
        ocr._engine = Mock(return_value=SimpleNamespace(
            boxes=[[(5, 22), (100, 22), (100, 80), (5, 80)]],
            txts=['[TEST] DemoPlayer'], scores=[.97],
        ))
        image = Image.new('RGB', (1920, 1200), 'white')
        self.assertIsNone(ocr.read_ranked_lobby_identity(image))
        ocr.read_lobby.assert_not_called()
        ocr.screen_hint.return_value = 'lobby'
        ocr.read_lobby.return_value = {**RANK, 'mode':'casual'}
        self.assertEqual(ocr.read_ranked_lobby_identity(image), {'mode':'casual'})
        ocr._engine.assert_not_called()
        ocr.read_lobby.return_value = RANK
        self.assertEqual(ocr.read_ranked_lobby_identity(image), {'name':'DemoPlayer','rank':RANK})
        height,width,channels = ocr._engine.call_args.args[0].shape
        self.assertEqual((height, width, channels), (141, 882, 3))

    def test_verified_lobby_can_recover_name_without_readable_rank(self):
        ocr = Recognizer()
        ocr.screen_hint = Mock(side_effect=AssertionError('already verified by scout'))
        ocr.read_lobby = Mock(return_value=parse_lobby('未上榜'))
        ocr._engine = Mock(side_effect=[
            SimpleNamespace(txts=None, scores=None),
            SimpleNamespace(boxes=[[(5, 22), (120, 22), (120, 80), (5, 80)]],
                            txts=['DemoPlayer'], scores=[.96]),
        ])
        image = Image.new('RGB', (1920, 1200), 'white')
        self.assertEqual(ocr.read_ranked_lobby_identity(image,verified_lobby=True),
                         {'name':'DemoPlayer','rank':None})
        self.assertGreater(ocr._engine.call_args.args[0].shape[1],882)

    def test_top_only_candidate_requires_two_lobby_navigation_labels(self):
        ocr = Recognizer()
        ocr.screen_hint = Mock(return_value='other')
        ocr.read_lobby = Mock(return_value=parse_lobby('未上榜'))
        ocr.read_lobby_name = Mock(return_value='DemoPlayer')
        ocr._engine = Mock(return_value=SimpleNamespace(txts=['游玩','商店'],scores=[.98,.82]))
        image = Image.new('RGB',(1920,1200),'white')
        self.assertIsNone(ocr.read_ranked_lobby_identity(image,candidate=True))
        ocr.read_lobby_name.assert_not_called()
        ocr._engine.return_value.scores[1]=.97
        self.assertEqual(ocr.read_ranked_lobby_identity(image,candidate=True),{'name':'DemoPlayer','rank':None})
        ocr.read_lobby_name.assert_called_once_with(image,expanded=True,candidate=True)


@unittest.skipUnless(sys.platform == 'win32', 'Windows capture boundary')
class FirstRunProfile(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.api = Api(Path(self.temp.name),desktop=True)
        self.addCleanup(self.api._store.db.close)
        self.clock = 1000
        self.api._profile_notifier = Mock()
        clock = patch('app.time.monotonic',side_effect=lambda:self.clock)
        clock.start()
        self.addCleanup(clock.stop)
        self.frame = Image.new('RGB',(1920,1200))
        self.api._ocr.screen_hint_regions = Mock(return_value='lobby')
        self.api._ocr.read_ranked_lobby_identity = Mock(return_value={'name':'DemoPlayer','rank':RANK})

    def step(self, game=GAME):
        with (patch('matchlog.windows_capture.foreground_game',return_value=game),
              patch('matchlog.windows_capture.capture_scout',return_value=(Mock(),Mock(),GAME)) as scout,
              patch('matchlog.windows_capture.capture_frame',return_value=(self.frame,GAME)) as capture):
            self.api._scan_profile_step()
        self.clock+=2
        return scout,capture

    def test_only_armed_foreground_lobby_is_captured(self):
        _,capture=self.step()
        capture.assert_not_called()
        self.api.begin_profile_scan()
        scout,capture=self.step(None)
        scout.assert_not_called()
        capture.assert_not_called()
        self.api._ocr.screen_hint_regions.return_value='settlement'
        _,capture=self.step()
        capture.assert_not_called()
        self.clock+=45
        self.step()
        self.assertEqual(self.api.state()['runtime']['profile_scan'],{'status':'expired'})
        self.assertIn('手动填写',self.api._profile_notifier.show.call_args.args[0].detail)

    def test_needs_two_observations_then_confirm_saves_rank_only(self):
        self.api.begin_profile_scan()
        self.step()
        self.assertEqual(self.api.state()['runtime']['profile_scan']['status'],'confirming')
        self.step()
        preview=self.api.state()
        self.assertEqual(preview['runtime']['profile_scan']['name'],'DemoPlayer')
        self.assertEqual(preview['runtime']['profile_scan']['total_rp'],2616)
        self.assertIn('返回 DiveTrail',self.api._profile_notifier.show.call_args.args[0].detail)
        self.assertEqual(preview['config']['self_name'],'')
        self.assertFalse((Path(self.temp.name)/'lobby').exists())
        result=self.api.confirm_initial_profile('DemoPlayer')
        self.assertEqual(result['config']['self_name'],'DemoPlayer')
        self.assertEqual(result['lobby_rank']['rank'],'青铜 I')
        self.assertEqual(result['rank_progress']['total_rp'],2616)
        self.assertEqual(result['matches'],[])
        with self.assertRaises(ValueError):
            self.api.confirm_initial_profile('DifferentPlayer')

    def test_manual_correction_never_attaches_another_names_candidate(self):
        self.api.begin_profile_scan()
        self.step()
        self.step()
        result=self.api.confirm_initial_profile('DifferentPlayer')
        self.assertEqual(result['config']['self_name'],'DifferentPlayer')
        self.assertIsNone(result['lobby_rank'])

    def test_wrong_mode_alert_once_then_accept_nickname_with_unstable_rank(self):
        self.api._ocr.read_ranked_lobby_identity.side_effect=[
            {'mode':'casual'},{'mode':'casual'},
            {'name':'DemoPlayer','rank':RANK},
            {'name':'DemoPlayer','rank':{**RANK,'progress':117}},
        ]
        self.api.begin_profile_scan()
        self.step()
        self.assertEqual(self.api.state()['runtime']['profile_scan']['status'],'wrong_mode')
        self.step()
        self.assertEqual(self.api._profile_notifier.show.call_count,1)
        self.step()
        self.step()
        scan=self.api.state()['runtime']['profile_scan']
        self.assertEqual((scan['status'],scan['name']),('ready','DemoPlayer'))
        self.assertIsNone(scan['total_rp'])
        result=self.api.confirm_initial_profile('DemoPlayer')
        self.assertIsNone(result['lobby_rank'])

    def test_lobby_change_needs_two_distinct_reads_and_does_not_write_until_confirmed(self):
        self.api.confirm_initial_profile('OldPlayer')
        ocr=self.api._ocr
        ocr.read_lobby_name=Mock(return_value='NewPlayer')
        self.assertFalse(self.api._check_lobby_profile(self.frame,100,GAME))
        self.assertFalse(self.api._check_lobby_profile(self.frame,100,GAME))
        self.assertNotIn('profile_change',self.api.state()['runtime'])
        self.assertFalse(self.api._check_lobby_profile(self.frame,102,GAME))
        state=self.api.state()
        self.assertEqual(state['runtime']['profile_change'],{'current':'OldPlayer','detected':'NewPlayer'})
        self.assertEqual(state['config']['self_name'],'OldPlayer')
        ocr.read_lobby_name.return_value='[TEAM] OldPlayer'
        self.assertTrue(self.api._check_lobby_profile(self.frame,103,GAME))
        self.assertNotIn('profile_change',self.api.state()['runtime'])
