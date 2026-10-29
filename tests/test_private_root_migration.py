from tests import isolation  # noqa: F401
import tempfile
import unittest
from pathlib import Path

from qwen_asr_app.paths import migrate_legacy_private_root


class PrivateRootMigrationTests(unittest.TestCase):
    def test_moves_once_without_overwriting_and_sets_junk_aside(self):
        with tempfile.TemporaryDirectory() as tmp:
            legacy, target = Path(tmp, 'legacy'), Path(tmp, 'local-data')
            (legacy / 'yuketang').mkdir(parents=True)
            (legacy / 'yuketang' / 'bank.json').write_text('{}')
            (legacy / 'ai-settings.json').write_text('old')
            (legacy / 'jobs.json').write_text(f'{{"path": "{legacy}/uploads/a.wav"}}')
            (legacy / 'diagnostics').mkdir()
            target.mkdir()
            (target / 'ai-settings.json').write_text('new')
            moved = migrate_legacy_private_root(legacy, target)
            self.assertEqual(moved, ['jobs.json', 'yuketang'])
            self.assertIn(str(target), (target / 'jobs.json').read_text())
            self.assertEqual((target / 'ai-settings.json').read_text(), 'new')
            self.assertTrue((target / '.legacy-junk' / 'diagnostics').is_dir())
            self.assertTrue((legacy / 'ai-settings.json').exists(), 'conflicting entries stay for review')
            (legacy / 'ai-settings.json').unlink()
            self.assertEqual(migrate_legacy_private_root(legacy, target), [])
            self.assertFalse(legacy.exists())


if __name__ == '__main__':
    unittest.main()
