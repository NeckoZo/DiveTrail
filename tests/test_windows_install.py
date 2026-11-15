"""Verify installed edition migration without touching the Windows Documents folder."""

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import ModuleType
from unittest import TestCase
from unittest.mock import Mock, patch

from matchlog.windows_install import MIGRATION_MARKER, installed_data_path
from matchlog.windows_startup import startup_command


class InstalledDataMigrationTests(TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.documents = Path(folder.name)
        self.legacy = self.documents / 'Apex MatchLog'
        self.target = self.documents / 'DiveTrail'
        docs = patch('matchlog.windows_install._documents_path', return_value=self.documents)
        docs.start()
        self.addCleanup(docs.stop)
        relocation = ModuleType('matchlog.data_relocation')
        self.rebase = relocation.rebase_database = Mock()
        module = patch.dict(sys.modules, {'matchlog.data_relocation': relocation})
        module.start()
        self.addCleanup(module.stop)

    def test_new_install_and_two_existing_folders(self):
        self.assertEqual(installed_data_path(), self.target)
        self.legacy.mkdir()
        self.target.mkdir()
        (self.legacy / 'old-file').touch()
        self.assertEqual(installed_data_path(), self.target)
        self.assertTrue((self.legacy / 'old-file').is_file())
        self.rebase.assert_not_called()

    def test_rebase_resumes_after_interrupted_move(self):
        self.legacy.mkdir()
        (self.legacy / 'matches.sqlite3').write_bytes(b'prior matches')
        self.rebase.side_effect = [OSError('interrupted'), None]
        self.assertEqual(installed_data_path(migrate=False), self.legacy)
        with self.assertRaisesRegex(OSError, 'interrupted'):
            installed_data_path()
        self.assertTrue((self.target / MIGRATION_MARKER).is_file())
        self.assertEqual((self.target / 'matches.sqlite3').read_bytes(), b'prior matches')
        self.assertEqual(installed_data_path(), self.target)
        self.assertFalse((self.target / MIGRATION_MARKER).exists())
        self.rebase.assert_called_with(self.target, self.legacy)
        self.assertEqual(self.rebase.call_count, 2)

    def test_legacy_directory_without_database(self):
        self.legacy.mkdir()
        (self.legacy / 'settings-example').touch()
        self.assertEqual(installed_data_path(), self.target)
        self.assertTrue((self.target / 'settings-example').is_file())
        self.assertFalse((self.target / MIGRATION_MARKER).exists())
        self.rebase.assert_not_called()

    def test_rename_failure_uses_previous_data_location(self):
        self.legacy.mkdir()
        (self.legacy / 'matches.sqlite3').touch()
        with patch.object(Path, 'rename', side_effect=PermissionError('locked')):
            self.assertEqual(installed_data_path(), self.legacy)
        self.assertTrue((self.legacy / 'matches.sqlite3').exists())
        self.assertFalse(self.target.exists())
        self.rebase.assert_not_called()

    def test_installed_startup_supports_legacy_and_current_launchers(self):
        self.target.mkdir()
        old_launcher = self.target / 'Apex MatchLog.exe'
        old_launcher.touch()
        with patch.object(sys, 'argv', ['matchlog-entry.py', '--installed']):
            self.assertIn('Apex MatchLog.exe', startup_command(self.target))
            (self.target / 'DiveTrail.exe').touch()
            self.assertIn('DiveTrail.exe', startup_command(self.target))
