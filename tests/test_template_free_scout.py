"""Public builds can detect text without distributing game screenshots."""
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from matchlog.recognition import Recognizer, scout_text_scene
from matchlog.rank_badges import lobby_badge_rank


def header_tokens(*labels):
    return [{'text': label, 'x': x, 'y': y, 'score': score}
            for label, x, y, score in labels]


class TemplateFreeScoutTests(unittest.TestCase):
    def test_scene_labels_must_appear_at_the_expected_locations(self):
        for expected, labels in (
            ('lobby', [('游玩', .22, .03, .99), ('挑战', .33, .03, .99), ('商店', .70, .03, .99)]),
            ('profile', [('数据', .38, .03, .99), ('账户进度', .56, .03, .99)]),
            ('intro', [('Λ排位赛', .14, .09, .88)]),
            ('settlement', [('总结', .45, .092, .99), ('经验值', .55, .092, .99)]),
            ('hud', [('10 剩余小队数量', .83, .044, .99)]),
            ('spectator', [('排位', .38, .03, .99), ('实况', .49, .03, .99),
                           ('总结', .58, .03, .99), ('剩余小队数量', .86, .042, .99)]),
            ('training', [('射击场', .91, .045, .99), ('剩余小队数量', .84, .062, .99)]),
            ('other', [('总结', .12, .013, .99), ('经验值', .18, .068, .99),
                       ('剩余小队数量', .89, .041, .80)]),
        ):
            with self.subTest(expected=expected):
                self.assertEqual(scout_text_scene(header_tokens(*labels)), expected)

    def test_header_does_not_wait_on_the_recognition_lock_or_retry_continuously(self):
        reader = Recognizer()
        reader._scout = Mock(requires_text=True)
        reader._scout.hint_regions.return_value = 'other'
        reader._text_scout = Mock(return_value='lobby')
        top = Image.new('RGB', (800, 120))
        with patch('matchlog.recognition.time.monotonic', side_effect=[10, 10, 11, 12]):
            with reader._lock:
                self.assertIsNone(reader.screen_hint_regions(top, None, (800, 800)))
            self.assertEqual(reader.screen_hint_regions(top, None, (800, 800)), 'lobby')
            self.assertIsNone(reader.screen_hint_regions(top, None, (800, 800)))
            self.assertEqual(reader.screen_hint_regions(top, None, (800, 800)), 'lobby')
        self.assertEqual(reader._text_scout.call_count, 2)

    def test_missing_hud_templates_leave_combat_blank_and_keep_rp(self):
        reader = Recognizer()
        reader._initialize = Mock()
        reader._detect_text = Mock(side_effect=[
            Mock(txts=['-38 排位']), Mock(txts=['1:12345:12345:deadbeef']),
        ])
        with patch.object(Path, 'is_file', return_value=False):
            result = reader.read_hud(Image.new('RGB', (800, 600)))
        self.assertIsNone(result['damage'])
        self.assertIsNone(result['kills'])
        self.assertEqual(result['live_rp'], -38)
        self.assertEqual(result['game_key'], '1:12345:12345:deadbeef')

    def test_missing_rank_art_keeps_lobby_numeral_fallback(self):
        with patch('matchlog.rank_badges._division', side_effect=FileNotFoundError), patch(
            'matchlog.rank_badges._lobby_numeral', return_value=2
        ):
            self.assertEqual(lobby_badge_rank(Image.new('RGB', (250, 120)), '黄金 III'), '黄金 II')
