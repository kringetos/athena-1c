import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from athena.catalog import Catalog
from athena.editing import prepare, import_command, upload_module, text_hash
from athena.exporter import Profiles, normalize
from athena.server import Workspace

DEMO = Path(__file__).resolve().parent.parent / 'examples/demo'


class EditingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        state_patch=patch('athena.server.STATE',self.root/'recent.json')
        state_patch.start();self.addCleanup(state_patch.stop)
        self.reference = self.root/'reference'; shutil.copytree(DEMO,self.reference)
        (self.reference/'ConfigDumpInfo.xml').write_text('<ConfigDumpInfo/>',encoding='utf-8')
        base=self.root/'base';base.mkdir();(base/'1Cv8.1CD').touch()
        exe=self.root/'1cv8.exe';exe.touch()
        self.profile=normalize({'name':'Тест редактирования','kind':'file','location':str(base),'platform':str(exe),'user':'Test'})
        self.workspace=Workspace();self.workspace.profiles=Profiles(self.root/'data')
        self.workspace.profiles.save(self.profile,self.reference)
        self.workspace.catalog=Catalog(self.reference).scan();self.workspace.active_connection=self.profile['id']
        self.workspace.passwords[self.profile['id']]='edit-secret'
        module=next(n for n in self.workspace.catalog.nodes.values() if n['kind']=='Module')
        self.file_id=module['files'][0]['id'];self.file=self.workspace.catalog.file(self.file_id)
        self.content=self.file['content']+'\n// NEW_EDITOR_CODE'

    def tearDown(self):
        self.workspace.close();self.temp.cleanup()

    def create(self):
        return self.workspace.prepare_change({'file_id':self.file_id,'content':self.content,'base_hash':self.file['hash']})

    def wait(self):
        deadline=time.monotonic()+5
        while self.workspace.job['status']=='running':
            if time.monotonic()>deadline:self.fail('Import did not finish')
            time.sleep(.01)
        # The manifest is written immediately after the published job state.
        deadline=time.monotonic()+2
        while any(d['status']=='submitting' for d in self.workspace.drafts.values()):
            if time.monotonic()>deadline:self.fail('Draft status did not finish')
            time.sleep(.01)

    def test_prepare_keeps_source_and_backup_and_writes_only_selected_bsl(self):
        before=(self.reference/self.file['path']).read_bytes()
        preview=self.create();draft=self.workspace.drafts[preview['id']]
        files=list((Path(draft['folder'])/'files').rglob('*'))
        self.assertEqual([p.name for p in files if p.is_file()],[Path(self.file['path']).name])
        self.assertEqual((self.reference/self.file['path']).read_bytes(),before)
        self.assertEqual(Path(preview['backup']).read_bytes(),before)
        self.assertIn('+// NEW_EDITOR_CODE',preview['diff'])
        self.assertNotIn('edit-secret',json.dumps(draft))

    def test_xml_wrong_hash_and_path_injection_are_rejected(self):
        xml_id=next(key for key,path in self.workspace.catalog.files.items() if path.suffix=='.xml')
        for key,content,version in [(xml_id,'test',self.file['hash']),('../outside','test',self.file['hash']),(self.file_id,self.content,'stale'),(self.file_id,'bad\x00code',self.file['hash'])]:
            with self.subTest(key=key),self.assertRaises(ValueError):
                self.workspace.prepare_change({'file_id':key,'content':content,'base_hash':version})
        with self.assertRaises(ValueError):self.workspace.prepare_change({'file_id':self.file_id,'content':self.file['content'],'base_hash':self.file['hash']})

    def test_command_has_single_file_scope_and_never_updates_database(self):
        profile={**self.profile,'kind':'server','location':'server/Test','extension':'MyExtension'}
        args=import_command(profile,'edit-secret','folder','folder/CommonModules/Test/Ext/Module.bsl','log','result')
        self.assertEqual(args[args.index('-files')+1],'folder/CommonModules/Test/Ext/Module.bsl')
        self.assertIn('-partial',args);self.assertEqual(args[args.index('-Extension')+1],'MyExtension')
        self.assertEqual(args[args.index('/S')+1],'server/Test')
        for bad in ['/UpdateDBCfg','/LoadCfg','/DumpConfigToFiles','-force','-NoCheck']:self.assertNotIn(bad,args)

    def test_remote_conflict_blocks_import_and_preserves_draft(self):
        preview=self.create()
        with patch('athena.server.configuration_changed',return_value=True),patch('athena.server.upload_module') as upload:
            self.workspace.apply_change({'id':preview['id']});self.wait()
        upload.assert_not_called();self.assertFalse(self.workspace.job['imported'])
        self.assertEqual(self.workspace.catalog.root,self.reference)
        self.assertTrue(Path(preview['backup']).is_file())

    def test_success_is_verified_by_readback_and_replay_is_rejected(self):
        preview=self.create();draft=self.workspace.drafts[preview['id']]
        exported=self.root/'exported';shutil.copytree(self.reference,exported)
        target=exported/self.file['path'];target.write_text(self.content,encoding='utf-8-sig')
        with patch('athena.server.STATE',self.root/'recent.json'),patch('athena.server.configuration_changed',return_value=False),patch('athena.server.upload_module') as upload,patch('athena.server.export_configuration',return_value=exported):
            self.workspace.apply_change({'id':preview['id']});self.wait()
        upload.assert_called_once();self.assertEqual(self.workspace.job['status'],'ready')
        self.assertTrue(self.workspace.job['imported']);self.assertEqual(draft['status'],'applied')
        self.assertEqual(self.workspace.catalog.file(self.file_id)['hash'],text_hash(self.content))
        with self.assertRaises(ValueError):self.workspace.apply_change({'id':preview['id']})

    def test_import_succeeded_but_readback_failed_is_reported_without_retry(self):
        preview=self.create()
        with patch('athena.server.configuration_changed',return_value=False),patch('athena.server.upload_module'),patch('athena.server.export_configuration',side_effect=ValueError('failed edit-secret')):
            self.workspace.apply_change({'id':preview['id']});self.wait()
        self.assertTrue(self.workspace.job['imported']);self.assertEqual(self.workspace.job['status'],'error')
        self.assertIn('1С подтвердила загрузку',self.workspace.job['error']);self.assertNotIn('edit-secret',self.workspace.job['error'])
        self.assertEqual(self.workspace.drafts[preview['id']]['status'],'needs_review')
        with self.assertRaises(ValueError):self.workspace.apply_change({'id':preview['id']})

    def test_zero_process_exit_without_dump_result_is_not_success(self):
        preview=self.create();draft=self.workspace.drafts[preview['id']]
        class Done:
            returncode=0
            def poll(self):return 0
        with patch('athena.editing.subprocess.Popen',return_value=Done()),self.assertRaises(ValueError):upload_module(self.profile,'edit-secret',draft)

    def test_batch_import_checks_result_and_masks_log(self):
        preview=self.create();draft=self.workspace.drafts[preview['id']]
        def launch(args,**kwargs):
            self.assertFalse(kwargs['shell'])
            self.assertTrue(Path(args[args.index('-files')+1]).is_file())
            Path(args[args.index('/DumpResult')+1]).write_text('0',encoding='utf-8-sig')
            Path(args[args.index('/Out')+1]).write_text('result edit-secret',encoding='utf-8')
            class Done:
                returncode=0
                def poll(self):return 0
            return Done()
        updates=[]
        with patch('athena.editing.subprocess.Popen',side_effect=launch):
            upload_module(self.profile,'edit-secret',draft,lambda **data:updates.append(data))
        self.assertNotIn('edit-secret',str(updates))
        self.assertNotIn('edit-secret',(Path(draft['folder'])/'import.log').read_text(encoding='utf-8'))

    def test_stage_tampering_and_changed_project_are_rejected(self):
        preview=self.create();draft=self.workspace.drafts[preview['id']]
        stage=Path(draft['folder'])/'files'/draft['path'];stage.write_text('tampered',encoding='utf-8')
        with patch('athena.editing.subprocess.Popen') as process,self.assertRaises(ValueError):upload_module(self.profile,'',draft)
        process.assert_not_called()
        self.workspace.catalog=Catalog(DEMO).scan()
        with self.assertRaises(ValueError):self.workspace.apply_change({'id':preview['id']})


if __name__=='__main__':unittest.main()
