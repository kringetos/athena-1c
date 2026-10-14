import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import xml.etree.ElementTree as ET

from athena.catalog import Catalog, child, local, read_text
from athena.exporter import Profiles, normalize
from athena.objects import prepare_object, validate_stage, object_import_command, verify_object
from athena.server import Workspace

DEMO=Path(__file__).resolve().parent.parent/'examples/demo'


class ObjectTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        state_patch=patch('athena.server.STATE',self.root/'recent.json')
        state_patch.start();self.addCleanup(state_patch.stop)
        self.source=self.root/'snapshot';shutil.copytree(DEMO,self.source)
        (self.source/'ConfigDumpInfo.xml').write_text('<ConfigDumpInfo/>',encoding='utf-8')
        (self.source/'Ext').mkdir();(self.source/'Ext'/'ParentConfigurations.bin').write_bytes(b'support settings fixture')
        self.catalog=Catalog(self.source).scan()
        base=self.root/'base';base.mkdir();(base/'1Cv8.1CD').touch();exe=self.root/'1cv8.exe';exe.touch()
        self.profile=normalize({'kind':'file','location':str(base),'platform':str(exe),'user':'Test'})
        self.spec={'kind':'Catalog','name':'СкладыAthena','synonym':'Склады & хранение',
            'attributes':[{'name':'Адрес','type':'String','length':0},{'name':'Активен','type':'Boolean'}],
            'sections':[{'name':'Ячейки','attributes':[{'name':'Объем','type':'Number','digits':15,'scale':0},
                {'name':'Товар','type':'CatalogRef.Номенклатура'},{'name':'Дата','type':'Date'}]}],
            'object_module':'// object code','manager_module':'// manager code'}

    def tearDown(self):self.temp.cleanup()

    def prepare(self,spec=None):return prepare_object(self.catalog,spec or self.spec,self.profile,self.root/'data')

    def test_package_preserves_source_support_settings_and_root_references(self):
        original={p.relative_to(self.source):p.read_bytes() for p in self.source.rglob('*') if p.is_file()}
        draft,preview=self.prepare();stage=Path(draft['folder'])/'files'
        self.assertEqual(original,{p.relative_to(self.source):p.read_bytes() for p in self.source.rglob('*') if p.is_file()})
        self.assertEqual((stage/'Ext/ParentConfigurations.bin').read_bytes(),b'support settings fixture')
        self.assertEqual((Path(draft['folder'])/'Configuration.before.xml').read_bytes(),original[Path('Configuration.xml')])
        config=child(ET.parse(stage/'Configuration.xml').getroot(),'Configuration')
        refs={(local(n.tag),n.text) for n in child(config,'ChildObjects')}
        self.assertIn(('Catalog','Номенклатура'),refs);self.assertIn(('Document','ЗаказПокупателя'),refs);self.assertIn(('Catalog',self.spec['name']),refs)
        self.assertEqual(len(draft['selected']),4);self.assertTrue(preview['can_upload']);validate_stage(draft)
        self.assertIn('&amp;',read_text(stage/draft['path']))

    def test_catalog_document_uuids_types_and_local_preview(self):
        for kind in ['Catalog','Document']:
            spec={**self.spec,'kind':kind};draft,preview=self.prepare(spec);stage=Path(draft['folder'])/'files'
            generated=Catalog(stage).scan();node=next(n for n in generated.nodes.values() if n['kind']==kind and n['name']==spec['name'])
            self.assertEqual(node['properties']['Synonym'],'ru · Склады & хранение')
            xml=ET.parse(stage/draft['path']).getroot();obj=child(xml,kind);all_ids=[e.get('uuid') for e in obj.iter() if e.get('uuid')]
            all_ids += [e.text for e in obj.iter() if local(e.tag) in {'TypeId','ValueId'}]
            self.assertEqual(len(all_ids),len(set(all_ids)));self.assertNotIn('00000000-0000-0000-0000-000000000000',all_ids)
            values=[e.text for e in obj.iter() if local(e.tag) in {'Length','FractionDigits'}];self.assertIn('0',values)
            self.assertIn('cfg:CatalogRef.Номенклатура',read_text(stage/draft['path']))
            # Native namespaces used inside QName text must remain declared.
            self.assertIn('xmlns:cfg=',read_text(stage/draft['path']));self.assertIn('xmlns:xs=',read_text(stage/draft['path']))

    def test_invalid_names_duplicates_types_and_limits_rejected(self):
        bad=[{'name':'../Outside'},{'name':'Номенклатура'},{'name':'номенклатура'},{'name':'7Bad'},{'name':'CON'},
             {'kind':'Role'},{'attributes':[{'name':'Код','type':'String'}]},
             {'attributes':[{'name':'A'},{'name':'a'}]}, {'attributes':[{'name':'A','type':'CatalogRef.Мissing'}]},
             {'attributes':[{'name':'A','type':'Number','digits':2,'scale':3}]},
             {'attributes':[{'name':'A','type':'String','length':True}]},
             {'sections':[{'name':'Адрес'}]},{'object_module':'bad\x00code'}]
        for change in bad:
            with self.subTest(change=change),self.assertRaises(ValueError):self.prepare({**self.spec,**change})

    def test_selected_object_import_and_tamper_detection(self):
        draft,_=self.prepare();args=object_import_command(self.profile,'secret',draft,'log','result')
        self.assertNotIn('-partial',args);self.assertNotIn('/UpdateDBCfg',args);self.assertNotIn('-NoCheck',args)
        paths=Path(args[args.index('-listFile')+1]).read_text(encoding='utf-8-sig').splitlines()
        self.assertEqual(len(paths),4);self.assertTrue(all(Path(p).is_file() for p in paths))
        (Path(draft['folder'])/'files'/draft['path']).write_text('tamper',encoding='utf-8')
        with self.assertRaises(ValueError):validate_stage(draft)

    def test_readback_rejects_lost_existing_object_and_wrong_module(self):
        draft,_=self.prepare();stage=Path(draft['folder'])/'files';verify_object(Catalog(stage).scan(),draft)
        output=self.root/'exported';shutil.copytree(stage,output)
        p=output/draft['selected'][2];p.write_text('changed code',encoding='utf-8')
        with self.assertRaises(ValueError):verify_object(Catalog(output).scan(),draft)
        p.write_bytes((stage/draft['selected'][2]).read_bytes())
        root=ET.parse(output/'Configuration.xml');config=child(root.getroot(),'Configuration');children=child(config,'ChildObjects')
        children.remove(next(n for n in children if local(n.tag)=='Document'));root.write(output/'Configuration.xml',encoding='utf-8')
        with self.assertRaises(ValueError):verify_object(Catalog(output).scan(),draft)

    def test_workspace_creation_success_conflict_and_local_only(self):
        workspace=Workspace();workspace.profiles=Profiles(self.root/'data');workspace.profiles.save(self.profile,self.source)
        workspace.catalog=self.catalog;workspace.active_connection=self.profile['id'];workspace.passwords[self.profile['id']]='secret'
        preview=workspace.prepare_object(self.spec);draft=workspace.drafts[preview['id']]
        with patch('athena.server.configuration_changed',return_value=False),patch('athena.server.upload_object') as upload,patch('athena.server.export_configuration',return_value=Path(draft['folder'])/'files'),patch('athena.server.STATE',self.root/'recent.json'):
            workspace.apply_change({'id':preview['id']})
            deadline=time.monotonic()+5
            while workspace.job['status']=='running' and time.monotonic()<deadline:time.sleep(.01)
        self.assertEqual(workspace.job['status'],'ready',workspace.job);upload.assert_called_once();self.assertEqual(draft['status'],'applied')
        workspace.catalog=self.catalog;workspace.active_connection=None
        local_package=workspace.prepare_object(self.spec);self.assertFalse(local_package['can_upload'])
        with self.assertRaises(ValueError):workspace.apply_change({'id':local_package['id']})
        workspace.close()

    def test_saved_remote_change_blocks_new_object_import(self):
        workspace=Workspace();workspace.catalog=self.catalog;workspace.profiles=Profiles(self.root/'data')
        workspace.profiles.save(self.profile,self.source);workspace.active_connection=self.profile['id'];workspace.passwords[self.profile['id']]='secret'
        preview=workspace.prepare_object(self.spec)
        with patch('athena.server.configuration_changed',return_value=True),patch('athena.server.upload_object') as upload:
            workspace.apply_change({'id':preview['id']})
            deadline=time.monotonic()+5
            while workspace.job['status']=='running' and time.monotonic()<deadline:time.sleep(.01)
        upload.assert_not_called();self.assertEqual(workspace.job['status'],'error');self.assertFalse(workspace.job['imported'])
        workspace.close()


if __name__=='__main__':unittest.main()
