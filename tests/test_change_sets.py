import json
import shutil
import tempfile
import unittest
from pathlib import Path
import xml.etree.ElementTree as ET
from athena.catalog import Catalog, read_text, text_hash, child, local
from athena.change_sets import ChangeSets, verify_set


class SetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        shutil.copytree(Path(__file__).resolve().parents[1] / 'examples' / 'demo', self.root / 'project')
        self.catalog = Catalog(self.root / 'project').scan(); self.store = ChangeSets(self.root / 'data')
        self.value = self.store.create('demo', 'Проверка наборов')
        self.modules = [(identity, path) for identity, path in self.catalog.files.items() if path.suffix == '.bsl']

    def tearDown(self): self.temp.cleanup()

    def add_module(self, position=0):
        identity, path = self.modules[position]; before = read_text(path)
        self.value = self.store.add(self.value['id'], 'demo', self.value['revision'], self.catalog, {'kind':'module','file_id':identity,'base_hash':text_hash(before),'content':before+'\n// Изменение для проверки\n'})

    def add_object(self, kind, name):
        self.value = self.store.add(self.value['id'], 'demo', self.value['revision'], self.catalog, {'kind':'object','spec':{'kind':kind,'name':name}})

    def prepare(self): return self.store.prepare(self.value['id'], 'demo', self.value['revision'], self.catalog, None)

    def test_multi_module_two_objects_merge_without_modifying_source(self):
        original = (self.catalog.root / 'Configuration.xml').read_bytes()
        self.add_module(); self.add_module(1); self.add_object('Catalog','ДоговорыТест'); self.add_object('Document','АктТест')
        result = self.prepare(); self.assertTrue(result['prepared']); package = result['set']['package']
        root = child(ET.parse(Path(package['folder']) / 'files' / 'Configuration.xml').getroot(),'Configuration')
        refs = {(local(c.tag),c.text) for c in child(root,'ChildObjects')}
        self.assertIn(('Catalog','ДоговорыТест'),refs); self.assertIn(('Document','АктТест'),refs)
        self.assertEqual((self.catalog.root/'Configuration.xml').read_bytes(),original)
        self.assertEqual(len(package['module_hashes']),2)
        self.store.check_package(package,self.catalog)
        verify_set(Catalog(Path(package['folder'])/'files').scan(),package)

    def test_persistence_exclusion_and_revision_conflicts(self):
        self.add_module(); restored = ChangeSets(self.root/'data')
        self.assertEqual(restored.read(self.value['id'])['entries'],self.value['entries'])
        revision = self.value['revision']
        self.value = restored.enable(self.value['id'],'demo',revision,self.value['entries'][0]['id'],False)
        self.assertFalse(self.prepare()['prepared'])
        with self.assertRaises(ValueError): restored.enable(self.value['id'],'demo',revision,self.value['entries'][0]['id'],True)
        with self.assertRaises(ValueError): restored.enable(self.value['id'],'wrong',self.value['revision'],self.value['entries'][0]['id'],True)

    def test_stale_base_and_syntax_errors_block_preparation(self):
        self.add_module(); self.modules[0][1].write_text('Процедура Изменено()\nКонецПроцедуры',encoding='utf-8')
        validation = self.store.validate(self.value,self.catalog)
        self.assertFalse(validation['valid']); self.assertIn('изменился',validation['checks'][0]['errors'][0])
        identity,path=self.modules[1]
        self.value=self.store.add(self.value['id'],'demo',self.value['revision'],self.catalog,{'kind':'module','file_id':identity,'base_hash':text_hash(read_text(path)),'content':'Процедура Незавершенная()'})
        self.assertTrue(self.store.validate(self.value,self.catalog)['checks'][1]['diagnostics'])
        self.assertFalse(self.prepare()['prepared'])

    def test_package_tamper_and_edit_after_prepare_block_upload(self):
        self.add_module(); package = self.prepare()['set']['package']
        changed = Path(package['folder'])/'files'/self.value['entries'][0]['path']; changed.write_text('tampered',encoding='utf-8')
        with self.assertRaises(ValueError): self.store.check_package(package,self.catalog)
        package = self.prepare()['set']['package']
        self.store.enable(self.value['id'],'demo',self.value['revision'],self.value['entries'][0]['id'],False)
        with self.assertRaises(ValueError): self.store.check_package(package,self.catalog)

    def test_loaded_package_and_unsafe_paths(self):
        self.add_module(); package = self.prepare()['set']['package']
        restored = ChangeSets(self.root/'data'); self.assertEqual(restored.prepared(package['id'])['id'],package['id'])
        self.store.mark(package,'needs_review')
        with self.assertRaises(ValueError): self.store.enable(self.value['id'],'demo',self.value['revision'],self.value['entries'][0]['id'],False)
        with self.assertRaises(ValueError): self.store.read('../outside')

    def test_archive_is_recoverable_and_invalidates_prepared_package(self):
        self.add_module(); package=self.prepare()['set']['package']
        value=self.store.archive(self.value['id'],'demo',self.value['revision'])
        with self.assertRaises(ValueError): self.store.check_package(package,self.catalog)
        restored=self.store.archive(value['id'],'demo',value['revision'],True)
        self.assertEqual(restored['status'],'draft');self.assertEqual(restored['entries'],self.value['entries'])

    def test_verify_detects_missing_or_changed_loaded_code(self):
        self.add_module(); package=self.prepare()['set']['package']; stage=Path(package['folder'])/'files'
        shutil.copytree(stage,self.root/'exported')
        (self.root/'exported'/self.value['entries'][0]['path']).write_text('different',encoding='utf-8')
        with self.assertRaises(ValueError): verify_set(Catalog(self.root/'exported').scan(),package)
