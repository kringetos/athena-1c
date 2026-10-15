import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from athena.catalog import Catalog
from athena.exporter import Profiles, normalize, configuration_changed
from athena.server import Workspace

DEMO = Path(__file__).resolve().parent.parent / 'examples/demo'


class AutoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        state_patch=patch('athena.server.STATE',self.root/'recent.json')
        state_patch.start();self.addCleanup(state_patch.stop)
        self.reference = self.root / 'reference'
        shutil.copytree(DEMO, self.reference)
        (self.reference / 'ConfigDumpInfo.xml').write_text('<ConfigDumpInfo/>', encoding='utf-8')
        base = self.root / 'base'; base.mkdir(); (base / '1Cv8.1CD').touch()
        exe = self.root / '1cv8.exe'; exe.touch()
        self.profile = normalize({'kind':'file', 'location':str(base), 'platform':str(exe), 'user':'Test'})
        self.workspace = Workspace()
        self.workspace.profiles = Profiles(self.root / 'data')
        self.workspace.profiles.save(self.profile, self.reference)
        self.workspace.catalog = Catalog(self.reference).scan()
        self.workspace.active_connection = self.profile['id']
        self.workspace.passwords[self.profile['id']] = 'secret-auto'
        self.workspace.auto['enabled'] = True

    def tearDown(self):
        self.workspace.close()
        if self.workspace.auto_thread: self.workspace.auto_thread.join(timeout=3)
        self.temp.cleanup()

    def fake_check(self, changes, write=True):
        def launch(args, **kwargs):
            self.assertNotIn('-update', args)
            destination = Path(args[args.index('/DumpConfigToFiles') + 1])
            self.assertNotEqual(destination, self.reference)
            self.assertFalse(list(destination.iterdir()))
            self.assertEqual(args[args.index('-configDumpInfoForChanges') + 1], str(self.reference / 'ConfigDumpInfo.xml'))
            self.assertLess(args.index('-getChanges'), args.index('/Out'))
            self.assertLess(args.index('-configDumpInfoForChanges'), args.index('/Out'))
            # An accidental full dump can only affect the temporary output.
            (destination / 'Configuration.xml').write_text('', encoding='utf-8')
            Path(args[args.index('/DumpResult') + 1]).write_text('0', encoding='utf-8')
            if write: Path(args[args.index('-getChanges') + 1]).write_text(changes, encoding='utf-8-sig')
            class Done:
                returncode = 0
                def poll(self): return 0
            return Done()
        return launch

    def test_changes_check_empty_bom_and_modified_without_touching_snapshot(self):
        old = {p.relative_to(self.reference):p.read_bytes() for p in self.reference.rglob('*') if p.is_file()}
        for content, expected in [('',False), ('Modified: CommonModule.Test',True), ('FullDump',True)]:
            with patch('athena.exporter.subprocess.Popen', side_effect=self.fake_check(content)):
                self.assertEqual(configuration_changed(self.profile,'secret-auto',self.reference), expected)
        self.assertEqual(old,{p.relative_to(self.reference):p.read_bytes() for p in self.reference.rglob('*') if p.is_file()})

    def test_missing_changes_result_is_error_not_unchanged(self):
        with patch('athena.exporter.subprocess.Popen', side_effect=self.fake_check('',write=False)):
            with self.assertRaises(ValueError): configuration_changed(self.profile,'',self.reference)

    def test_no_changes_skip_export_and_keep_revision_and_catalog(self):
        previous = self.workspace.catalog
        with patch('athena.server.configuration_changed',return_value=False), patch('athena.server.export_configuration') as export:
            self.workspace.auto_tick()
        export.assert_not_called()
        self.assertIs(self.workspace.catalog,previous)
        self.assertEqual(self.workspace.revision,0)
        self.assertEqual(self.workspace.auto['phase'],'Изменений нет')

    def test_saved_code_change_publishes_new_catalog_and_keeps_previous_snapshot(self):
        new = self.root / 'new'; shutil.copytree(self.reference,new)
        module = next(n for n in self.workspace.catalog.nodes.values() if n['kind']=='Module')
        file_id = module['files'][0]['id']
        original = self.workspace.catalog.file(file_id)['content']
        path = new / module['files'][0]['path']
        path.write_text(original+'\n// automatic update marker',encoding='utf-8')
        with patch('athena.server.STATE',self.root/'recent.json'), patch('athena.server.configuration_changed',return_value=True), patch('athena.server.export_configuration',return_value=new) as export:
            self.workspace.auto_tick()
        self.assertEqual(export.call_args.args[1],'secret-auto')
        self.assertIn('automatic update marker',self.workspace.catalog.file(file_id)['content'])
        self.assertNotIn('automatic update marker',(self.reference / module['files'][0]['path']).read_text(encoding='utf-8-sig'))
        self.assertEqual(self.workspace.revision,1)
        self.assertEqual(self.workspace.profiles.all()[0]['last_export'],str(new))

    def test_failure_redacts_password_and_preserves_previous_catalog(self):
        previous=self.workspace.catalog
        with patch('athena.server.configuration_changed',side_effect=ValueError('Failed secret-auto')):
            self.workspace.auto_tick()
        self.assertIs(self.workspace.catalog,previous)
        self.assertEqual(self.workspace.job['status'],'ready')
        self.assertNotIn('secret-auto',self.workspace.auto['error'])

    def test_busy_or_disabled_never_starts_another_process(self):
        with patch('athena.server.configuration_changed') as check:
            self.workspace.job={'status':'running'}; self.workspace.auto_tick()
            self.workspace.job={'status':'ready'}; self.workspace.auto['enabled']=False; self.workspace.auto_tick()
        check.assert_not_called()

    def test_password_from_settings_is_session_only_and_not_in_status(self):
        self.workspace.passwords.clear()
        with patch.object(self.workspace, 'watch'):
            response = self.workspace.configure_auto({'enabled':True,'interval':30,'password':'new-session-secret'})
        self.assertTrue(response['enabled'])
        self.assertEqual(self.workspace.passwords[self.profile['id']],'new-session-secret')
        self.assertNotIn('new-session-secret',str(response))
        self.assertNotIn('new-session-secret',self.workspace.profiles.path.read_text(encoding='utf-8'))

    def test_enable_without_password_and_disable_cancels_only_auto_job(self):
        self.workspace.passwords.clear()
        with patch.object(self.workspace,'watch'):
            response=self.workspace.configure_auto({'enabled':True,'interval':60})
        self.assertTrue(response['enabled'])
        with patch('athena.server.configuration_changed',return_value=False) as check:
            self.workspace.auto_tick()
        self.assertEqual(check.call_args.args[1],'')
        self.workspace.job={'status':'running','operation':'export','cancellable':True}
        self.workspace.configure_auto({'enabled':False,'interval':60})
        self.assertFalse(self.workspace.cancel_export.is_set())
        self.workspace.job['operation']='autosync'
        self.workspace.configure_auto({'enabled':False,'interval':60})
        self.assertTrue(self.workspace.cancel_export.is_set())
        with self.assertRaises(ValueError): self.workspace.configure_auto({'enabled':False,'interval':1})


if __name__ == '__main__': unittest.main()
