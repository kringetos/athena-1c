import json
import shutil
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from athena.server import Workspace
from athena.agent_tools import AgentTools, definitions
from athena.catalog import read_text, text_hash


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        self.state=patch('athena.server.STATE', self.root/'recent.json'); self.state.start()
        shutil.copytree(Path(__file__).resolve().parents[1]/'examples'/'demo',self.root/'project')
        self.w=Workspace(); self.w.index(self.root/'project',lambda *args,**kwargs:None)
        self.tools=self.w.agent_tools
        self.file_id,self.file_path=next((i,p) for i,p in self.w.catalog.files.items() if p.suffix=='.bsl')

    def tearDown(self):
        self.w.close(); self.state.stop(); self.temp.cleanup()

    def request(self,tool,arguments,**kwargs):
        context=self.tools.context(); context={k:context[k] for k in ['project','snapshot','workspace_revision']}
        return {'call_id':uuid.uuid4().hex,'tool':tool,'arguments':arguments,'context':context}|kwargs

    def call(self,tool,arguments):
        result=self.tools.call(self.request(tool,arguments)); self.assertTrue(result['ok'],result)
        return result['result']

    def create(self): return self.call('create_change_set',{'title':'Предложения агента'})

    def test_edit_validate_prepare_does_not_write_project(self):
        before=self.file_path.read_bytes(); config=(self.w.catalog.root/'Configuration.xml').read_bytes()
        value=self.create(); source=self.call('read_module',{'file_id':self.file_id})
        old=read_text(self.file_path).splitlines()[0]
        value=self.call('edit_module',{'set_id':value['id'],'revision':value['revision'],'file_id':self.file_id,'expected_hash':source['hash'],'edits':[{'old_text':old,'new_text':'// Предложение агента\n'+old}]})
        draft=self.call('read_module',{'file_id':self.file_id,'set_id':value['id']})
        self.assertTrue(draft['draft']); self.assertIn('Предложение агента',draft['content'])
        self.assertNotEqual(source['hash'],draft['hash'])
        check=self.call('validate_changes',{'set_id':value['id'],'revision':value['revision']})
        self.assertTrue(check['valid'])
        prepared=self.call('prepare_changes',{'set_id':value['id'],'revision':value['revision']})
        self.assertTrue(prepared['prepared']); self.assertTrue(prepared['requires_user_review']); self.assertFalse(prepared['can_upload'])
        self.assertEqual(self.file_path.read_bytes(),before); self.assertEqual((self.w.catalog.root/'Configuration.xml').read_bytes(),config)
        self.assertNotIn('content',value['entries'][0])

    def test_retry_persists_and_call_id_cannot_change_arguments(self):
        request=self.request('create_change_set',{'title':'Идемпотентность'})
        first=self.tools.call(request)
        tools=AgentTools(self.w,self.root)
        repeated=tools.call(request)
        self.assertTrue(repeated['replayed']); self.assertEqual(first['result'],repeated['result'])
        request['arguments']['title']='Другое название'
        self.assertFalse(tools.call(request)['ok'])
        self.assertEqual(len(self.w.change_sets.list(first['context']['project'])),1)

    def test_snapshot_project_and_revision_guards(self):
        for key,value in [('project','wrong'),('snapshot','wrong'),('workspace_revision',999)]:
            request=self.request('create_change_set',{'title':'Нельзя создать'}); request['context'][key]=value
            self.assertEqual(self.tools.call(request)['error']['code'],'stale_context')
        self.w.job={'status':'running'}
        self.assertFalse(self.tools.call(self.request('create_change_set',{'title':'Занято'}))['ok'])
        self.assertEqual(self.w.change_sets.list(str(self.w.catalog.root)),[])

    def test_schema_rejects_extra_arguments_bool_integer_and_unknown_tool(self):
        for tool,args in [('read_module',{'file_id':self.file_id,'start_line':True}),('read_module',{'file_id':self.file_id,'path':'../../secret'}),('apply_to_1c',{}),('get_calls',{'symbol_id':'x','direction':'other'}),('create_object',{'set_id':'x','revision':1,'spec':{'kind':'Catalog','name':'X','evil':1}})]:
            self.assertFalse(self.tools.call(self.request(tool,args))['ok'])
        self.assertEqual(len(definitions()),18)

    def test_complete_metadata_pages_and_counts_do_not_depend_on_code_search(self):
        from collections import Counter
        root=self.call('get_configuration',{})
        expected=[n for n in self.w.catalog.nodes.values() if n.get('parent')==root['id'] and n['kind'] not in {'Module','File'}]
        self.assertEqual(root['total'],len(expected))
        first=self.call('list_objects',{'limit':1})
        second=self.call('list_objects',{'offset':1,'limit':1})
        self.assertEqual(first['total'],len(expected))
        self.assertNotEqual(first['items'][0]['id'],second['items'][0]['id'])
        for node in expected:
            details=self.call('get_object',{'object_id':node['id'],'limit':1})
            counts=dict(Counter(n['kind'] for n in self.w.catalog.nodes.values() if n.get('parent')==node['id']))
            self.assertEqual(details['child_counts'],counts)
            page=self.call('list_objects',{'kinds':[node['kind']]})
            listed=next(n for n in page['items'] if n['id']==node['id'])
            self.assertEqual(listed['child_counts'],counts)

    def test_edit_is_atomic_on_ambiguous_replacement_and_hash_conflict(self):
        value=self.create(); args={'set_id':value['id'],'revision':value['revision'],'file_id':self.file_id,'expected_hash':text_hash(read_text(self.file_path)),'edits':[{'old_text':read_text(self.file_path).splitlines()[0],'new_text':'// Начало'},{'old_text':'Не существует','new_text':'Замена'}]}
        self.assertFalse(self.tools.call(self.request('edit_module',args))['ok'])
        self.assertEqual(self.w.change_sets.read(value['id'])['entries'],[])
        args['edits']=args['edits'][:1]; args['expected_hash']='0'*64
        self.assertFalse(self.tools.call(self.request('edit_module',args))['ok'])

    def test_new_object_and_revision_conflict(self):
        value=self.create(); args={'set_id':value['id'],'revision':value['revision'],'spec':{'kind':'Catalog','name':'ПоставщикиАгента','attributes':[{'name':'ИНН','type':'String','length':12}],'sections':[{'name':'Адреса','attributes':[{'name':'Адрес'}]}]}}
        result=self.call('create_object',args); self.assertEqual(len(result['entries']),1)
        self.assertFalse(self.tools.call(self.request('create_object',args))['ok'])
        args['revision']=result['revision']
        self.assertFalse(self.tools.call(self.request('create_object',args))['ok'])
        args['entry_id']=result['entries'][0]['id']; args['spec']['object_module']='Процедура Проверка()\nКонецПроцедуры'
        result=self.call('update_new_object',args)
        self.assertTrue(self.call('prepare_changes',{'set_id':result['id'],'revision':result['revision']})['prepared'])

    def test_cross_project_set_and_changed_source_block_edit(self):
        foreign=self.w.change_sets.create('another','Чужой')
        self.assertFalse(self.tools.call(self.request('read_change_set',{'set_id':foreign['id']}))['ok'])
        value=self.create(); original=read_text(self.file_path); line=original.splitlines()[0]
        value=self.call('edit_module',{'set_id':value['id'],'revision':value['revision'],'file_id':self.file_id,'expected_hash':text_hash(original),'edits':[{'old_text':line,'new_text':'// Черновик\n'+line}]})
        draft=self.call('read_module',{'file_id':self.file_id,'set_id':value['id']})
        self.file_path.write_text(original+'\n// Пользователь',encoding='utf-8')
        self.assertFalse(self.tools.call(self.request('edit_module',{'set_id':value['id'],'revision':value['revision'],'file_id':self.file_id,'expected_hash':draft['hash'],'edits':[{'old_text':'// Черновик','new_text':'// Другой'}]}))['ok'])

    def test_search_symbols_calls_and_bounded_output(self):
        result=self.call('search',{'query':'СуммаЗаказа','scope':'code'})
        symbol=result['groups']['code'][0]
        method=self.call('get_symbol',{'symbol_id':symbol['id']})
        self.assertEqual(method['item']['name'],'СуммаЗаказа')
        calls=self.call('get_calls',{'symbol_id':symbol['id'],'direction':'incoming','limit':1})
        self.assertLessEqual(len(calls['items']),1)
        node=next(n for n in self.w.catalog.nodes.values() if n['kind']=='Configuration')
        self.assertEqual(self.call('get_object',{'object_id':node['id'],'limit':1})['node']['id'],node['id'])
        self.assertLess(len(json.dumps(method)),250000)
