import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from athena.exporter import Profiles, normalize, command, export_configuration, ExportCancelled, registered_bases
from athena.server import Workspace

DEMO = Path(__file__).resolve().parent.parent / 'examples' / 'demo'


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        state_patch=patch('athena.server.STATE',self.root/'recent.json')
        state_patch.start();self.addCleanup(state_patch.stop)
        self.base = self.root / 'Моя база'
        self.base.mkdir()
        (self.base / '1Cv8.1CD').touch()
        self.exe = self.root / 'Платформа с пробелами' / '1cv8.exe'
        self.exe.parent.mkdir()
        self.exe.touch()
        self.data = {'name': 'Тестовый проект', 'kind': 'file', 'location': str(self.base), 'platform': str(self.exe), 'user': 'Разработчик', 'password': 'test-secret', 'extension': ''}
        self.profile = normalize(self.data)

    def tearDown(self):
        self.temp.cleanup()

    def fake_success(self, args, **kwargs):
        self.assertFalse(kwargs['shell'])
        destination = Path(args[args.index('/DumpConfigToFiles') + 1])
        shutil.copytree(DEMO, destination, dirs_exist_ok=True)
        Path(args[args.index('/Out') + 1]).write_text('Выгрузка завершена: test-secret', encoding='utf-8')
        Path(args[args.index('/DumpResult') + 1]).write_text('0', encoding='utf-8-sig')
        class Process:
            returncode = 0
            def poll(self): return 0
        return Process()

    def test_full_export_then_automatic_open_and_session_password(self):
        workspace = Workspace()
        workspace.profiles = Profiles(self.root / 'data')
        with patch('athena.server.STATE', self.root / 'recent.json'), patch('athena.exporter.subprocess.Popen', side_effect=self.fake_success) as process:
            workspace.export(self.data)
            self.wait(workspace)
            self.assertEqual(workspace.job['status'], 'ready')
            self.assertEqual(workspace.catalog.summary()['name'], 'ДемонстрацияAthena')
            first = workspace.catalog.root
            self.assertEqual(workspace.active_connection, self.profile['id'])
            persisted = workspace.profiles.path.read_text(encoding='utf-8')
            self.assertNotIn('test-secret', persisted)
            self.assertNotIn('password', persisted)
            self.assertNotIn('test-secret', workspace.job['log'])
            self.assertNotIn('test-secret', (first.parent / '1c.log').read_text(encoding='utf-8'))
            workspace.export({**self.data, 'password': ''})
            self.wait(workspace)
            self.assertEqual(workspace.job['status'], 'ready')
            self.assertNotEqual(workspace.catalog.root, first)
            self.assertTrue((first / 'Configuration.xml').is_file())
            second_args = process.call_args[0][0]
            self.assertEqual(second_args[second_args.index('/P') + 1], 'test-secret')

    def wait(self, workspace):
        deadline = time.monotonic() + 5
        while workspace.job['status'] == 'running':
            if time.monotonic() > deadline: self.fail('Export did not finish')
            time.sleep(.01)

    def test_server_extension_and_os_auth_commands_are_export_only(self):
        profile = normalize({**self.data, 'kind': 'server', 'location': 'srv:1541\\База', 'extension': 'МоёРасширение', 'os_auth': True})
        args = command(profile, 'must-not-appear', 'out', 'log', 'result')
        self.assertEqual(args[args.index('/S') + 1], 'srv:1541/База')
        self.assertIn('/WA+', args)
        self.assertNotIn('/N', args)
        self.assertNotIn('/P', args)
        self.assertEqual(args[args.index('-Extension') + 1], 'МоёРасширение')
        self.assertEqual(args[args.index('-Format') + 1], 'Hierarchical')
        self.assertFalse(any(arg in args for arg in ['/LoadCfg', '/UpdateDBCfg', '/LoadConfigFromFiles']))

    def test_zero_exit_without_result_is_not_a_success(self):
        def fake(args, **kwargs):
            process = self.fake_success(args, **kwargs)
            Path(args[args.index('/DumpResult') + 1]).unlink()
            return process
        with patch('athena.exporter.subprocess.Popen', side_effect=fake), self.assertRaises(ValueError):
            export_configuration(self.profile, 'test-secret', self.root / 'data')

    def test_cancel_terminates_only_the_owned_process(self):
        class Process:
            returncode = None
            terminated = False
            def poll(self): return self.returncode
            def terminate(self): self.terminated = True; self.returncode = -1
            def wait(self, timeout): return self.returncode
        process = Process()
        cancel = threading.Event(); cancel.set()
        with patch('athena.exporter.subprocess.Popen', return_value=process), self.assertRaises(ExportCancelled):
            export_configuration(self.profile, '', self.root / 'data', cancel=cancel)
        self.assertTrue(process.terminated)

    def test_failure_keeps_previous_catalog(self):
        workspace = Workspace(); workspace.profiles = Profiles(self.root / 'data')
        previous = object(); workspace.catalog = previous
        with patch('athena.exporter.subprocess.Popen', side_effect=OSError('Failed test-secret')):
            workspace.export(self.data); self.wait(workspace)
        self.assertEqual(workspace.job['status'], 'error')
        self.assertIs(workspace.catalog, previous)
        self.assertNotIn('test-secret', workspace.job['error'])

    def test_invalid_platform_base_and_server_are_rejected(self):
        for change in [{'platform': str(self.exe.parent / '1cv8c.exe')}, {'location': str(self.root)}, {'kind': 'server', 'location': '/LoadCfg'}, {'user': '/LoadCfg'}]:
            with self.subTest(change=change), self.assertRaises(ValueError): normalize({**self.data, **change})

    def test_launcher_list_ignores_credentials_and_web_bases(self):
        path = self.root / '1C/1CEStart/ibases.v8i'; path.parent.mkdir(parents=True)
        path.write_text('[Файловая]\nConnect=File="C:\\Базы\\База";Usr="Пользователь";Pwd="secret";\n[Серверная]\nConnect=Srvr="server";Ref="База";\n[Веб]\nConnect=ws="https://example.invalid";\n', encoding='utf-16')
        with patch.dict(os.environ, {'APPDATA': str(self.root)}): bases = registered_bases()
        self.assertEqual(len(bases), 2)
        self.assertEqual(bases[1]['location'], 'server/База')
        self.assertNotIn('secret', json.dumps(bases))


if __name__ == '__main__':
    unittest.main()
