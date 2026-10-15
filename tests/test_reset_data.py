from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from athena import reset_data


class ResetDataTests(unittest.TestCase):
    def test_reset_preserves_runtime_and_external_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder).resolve()
            state = project / '.athena'
            for name in ('exports/test/Configuration.xml', 'models/model.bin',
                         'gpu-runtime/runtime.bin', 'git/repos/source.bsl',
                         'git/sources.sqlite3', 'accounts.sqlite3-wal', 'user.json'):
                path = state / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('test')
            external = project / 'original-1c'
            external.mkdir()
            with patch.object(reset_data, '__file__', str(project / 'athena/reset_data.py')), \
                 patch.object(reset_data, 'check_stopped'), patch('builtins.input', return_value='RESET'), \
                 patch('builtins.print'):
                reset_data.main()
            self.assertFalse((state / 'exports').exists())
            self.assertFalse((state / 'accounts.sqlite3-wal').exists())
            self.assertFalse((state / 'git/sources.sqlite3').exists())
            self.assertTrue((state / 'models/model.bin').exists())
            self.assertTrue((state / 'gpu-runtime/runtime.bin').exists())
            self.assertTrue((state / 'git/repos/source.bsl').exists())
            self.assertTrue(external.exists())

    def test_redirected_target_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            state = root / '.athena'
            state.mkdir()
            outside = root / 'outside'
            outside.mkdir()
            try:
                (state / 'exports').symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest('Creating symlinks requires Windows developer mode')
            with self.assertRaises(ValueError):
                reset_data.targets(state)

    def test_active_service_blocks_reset(self):
        with patch('socket.socket') as socket:
            socket.return_value.__enter__.return_value.connect_ex.return_value = 0
            with self.assertRaises(ValueError):
                reset_data.check_stopped()
