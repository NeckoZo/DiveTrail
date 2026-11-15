import json
from pathlib import Path
import re
import unittest

from tools.build_changelog import render_changelog

ROOT = Path(__file__).resolve().parents[1]


class ChangelogParity(unittest.TestCase):
    def test_github_notes_match_in_app_notes(self):
        data = json.loads((ROOT / 'ui/changelog.json').read_text(encoding='utf-8'))
        self.assertEqual((ROOT / 'CHANGELOG.md').read_text(encoding='utf-8'), render_changelog(data))
        self.assertEqual(data['version'], data['releases'][0]['version'])
        self.assertRegex(
            (ROOT / 'ui/index.html').read_text(encoding='utf-8'),
            r'id="changelog-btn"[^>]*>\s*' + re.escape(data['version']) + r'\s*</button>',
        )


if __name__ == '__main__':
    unittest.main()
