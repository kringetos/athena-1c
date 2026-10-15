import json
import io
import sys
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from athena.server import Workspace, handler, LocalServer, main
from athena.exporter import Profiles


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state_patch = patch('athena.server.STATE', Path(self.temp.name) / 'recent.json')
        self.state_patch.start()
        self.workspace = Workspace()
        self.workspace.profiles = Profiles(Path(self.temp.name) / 'connections')
        self.server = LocalServer(('127.0.0.1', 0), handler(self.workspace))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.workspace.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.state_patch.stop()
        self.temp.cleanup()

    def request(self, path, body=None, authenticated=True):
        headers = {'X-Athena-Token': self.workspace.token} if authenticated else {}
        data = json.dumps(body).encode() if body is not None else None
        with urlopen(Request(self.url + path, data=data, headers=headers), timeout=5) as response:
            return json.loads(response.read())

    def test_ui_token_and_api_access(self):
        with urlopen(self.url) as response:
            html = response.read().decode()
        self.assertIn(self.workspace.token, html)
        self.assertNotIn('__ATHENA_TOKEN__', html)
        with self.assertRaises(HTTPError) as result:
            self.request('/api/state', authenticated=False)
        self.assertEqual(result.exception.code, 403)
        with self.assertRaises(HTTPError):
            self.request('/../README.md')
        for route in ['/api/prepare-change','/api/apply-change','/api/prepare-object']:
            with self.assertRaises(HTTPError) as denied:
                self.request(route,{},authenticated=False)
            self.assertEqual(denied.exception.code,403)

    def test_documentation_routes_require_token_and_keep_documents_separate(self):
        from athena.ai import DEFAULTS
        for path,body in [('/api/documents',None),('/api/documents/job?id=x',None),('/api/documents/start',{}),('/api/documents/cancel',{}),('/api/documents/save',{})]:
            with self.assertRaises(HTTPError) as denied:self.request(path,body,authenticated=False)
            self.assertEqual(denied.exception.code,403)
        self.workspace.index(Path(__file__).resolve().parents[1]/'examples'/'demo',lambda *a,**k:None)
        self.workspace.ai.configure({'provider':'ollama',**DEFAULTS['ollama'],'model':'doc-test'})
        context=self.request('/api/agent/context')
        with patch('athena.documentation.generate',return_value=('# Документ\nОписание исходников.',False)):
            started=self.request('/api/documents/start',{'title':'Документ','kind':'Архитектура','scope':'project',
                'context':{k:context[k] for k in ['project','snapshot','workspace_revision']}})
            deadline=time.monotonic()+3
            while self.workspace.ai.active and time.monotonic()<deadline:time.sleep(.01)
        job=self.request('/api/documents/job?id='+started['id'])
        self.assertEqual(job['status'],'ready',job)
        self.assertEqual(self.request('/api/documents')['documents'][0]['id'],job['id'])
        saved=self.request('/api/documents/save',{'id':job['id'],'content':'# Новая редакция'})
        self.assertEqual(saved['content'],'# Новая редакция')
        self.assertEqual(self.request('/api/change-sets')['sets'],[])
        with urlopen(self.url+'/documentation.js') as response:
            self.assertIn('documentation-dialog',response.read().decode())

    def test_agent_routes_authentication_and_protocol(self):
        for path,body in [('/api/agent/tools',None),('/api/agent/context',None),('/api/agent/history',None),('/api/agent/call',{})]:
            with self.assertRaises(HTTPError) as denied: self.request(path,body,authenticated=False)
            self.assertEqual(denied.exception.code,403)
        catalog=self.request('/api/agent/tools')
        self.assertEqual(len(catalog['tools']),18)
        self.assertEqual(len(catalog['modes']['analyze']),9)
        self.assertEqual(catalog['automatic'],['create_change_set'])
        self.assertFalse(self.request('/api/agent/context')['ready'])
        self.assertFalse(self.request('/api/agent/call',{})['ok'])
        with urlopen(self.url+'/agent-tools') as response:
            self.assertIn(self.workspace.token,response.read().decode())

    def test_code_agent_http_routes_and_task_lifecycle(self):
        for path,body in [('/api/agent/status',None),('/api/agent/job?id=x',None),('/api/agent/start',{}),('/api/agent/cancel',{}),('/api/agent/settings',{})]:
            with self.assertRaises(HTTPError) as denied:self.request(path,body,authenticated=False)
            self.assertEqual(denied.exception.code,403)
        self.workspace.index(Path(__file__).resolve().parents[1]/'examples'/'demo',lambda *args,**kwargs:None)
        from athena.ai import DEFAULTS
        self.workspace.ai.configure({'provider':'ollama',**DEFAULTS['ollama'],'model':'test'})
        context=self.request('/api/agent/context');context={k:context[k] for k in ['project','snapshot','workspace_revision']}
        answer=({'role':'assistant','content':'Изучено.'},False,{'input_tokens':1,'output_tokens':1})
        with patch('athena.code_agent.model_turn',return_value=answer):
            started=self.request('/api/agent/start',{'goal':'Изучи код','mode':'analyze','context':context})
            deadline=time.monotonic()+3
            while self.workspace.code_agent.active:
                self.assertLess(time.monotonic(),deadline);time.sleep(.01)
        result=self.request('/api/agent/job?id='+started['id']);self.assertEqual(result['status'],'ready');self.assertEqual(result['answer'],'Изучено.')
        saved=self.request('/api/agent/settings',{**self.workspace.code_agent.config,'max_steps':3})
        self.assertEqual(saved['settings']['max_steps'],3)

    def test_ai_routes_settings_and_context(self):
        state=self.request('/api/ai/settings')
        self.assertEqual(set(state['profiles']),{'ollama','openai'})
        settings=dict(state['profiles']['openai'],provider='openai',api_key='test-secret')
        saved=self.request('/api/ai/settings',settings)
        self.assertTrue(saved['profiles']['openai']['has_api_key'])
        self.assertNotIn('test-secret',json.dumps(saved))
        self.workspace.index(Path(__file__).resolve().parents[1]/'examples'/'demo',lambda *args,**kwargs:None)
        job=self.request('/api/ai/ask',{'question':'СуммаЗаказа','context_only':True})
        deadline=time.monotonic()+3
        while True:
            result=self.request('/api/ai/job?id='+job['id'])
            if result['status']!='running':break
            self.assertLess(time.monotonic(),deadline)
            time.sleep(.02)
        self.assertEqual(result['kind'],'context')
        self.assertTrue(result['sources'])
        self.assertEqual(result['snapshot'],self.workspace.knowledge_snapshot)

    def test_ai_inventory_uses_metadata_instead_of_function_search(self):
        self.workspace.index(Path(__file__).resolve().parents[1]/'examples'/'demo',lambda *args,**kwargs:None)
        root = next(n['id'] for n in self.workspace.catalog.nodes.values() if n['kind']=='Configuration')
        self.workspace.catalog.add('no-code-document','ДокументБезМодуля','Document',root,
                                   source='Documents/ДокументБезМодуля.xml')
        self.workspace.knowledge_snapshot = self.workspace.knowledge.index(self.workspace.catalog,'test')
        with patch.object(self.workspace,'search_functions',side_effect=AssertionError('Code search must not run')):
            started=self.request('/api/ai/ask',{'question':'Какие есть документы?'})
            deadline=time.monotonic()+3
            while True:
                result=self.request('/api/ai/job?id='+started['id'])
                if result['status']!='running':break
                self.assertLess(time.monotonic(),deadline)
                time.sleep(.01)
        expected={n['name'] for n in self.workspace.catalog.nodes.values() if n['kind']=='Document'}
        self.assertEqual(result['kind'],'metadata')
        self.assertEqual({s['name'] for s in result['sources']},expected)
        self.assertTrue(result['inventory_complete'])
        self.assertIn('ДокументБезМодуля',result['answer'])

    def test_ai_routes_require_token(self):
        for route,body in [('/api/assistant/ask',{}),('/api/ai/settings',None),('/api/ai/job?id=x',None),('/api/ai/settings',{}),('/api/ai/models',{}),('/api/ai/ask',{}),('/api/ai/cancel',{})]:
            with self.assertRaises(HTTPError) as denied:self.request(route,body,authenticated=False)
            self.assertEqual(denied.exception.code,403)

    def test_assistant_routes_counts_locally_and_other_questions_to_read_only_tools(self):
        self.workspace.index(Path(__file__).resolve().parents[1]/'examples'/'demo',lambda *args,**kwargs:None)
        with patch('athena.code_agent.model_turn',side_effect=AssertionError('No LLM for metadata counts')):
            started=self.request('/api/assistant/ask',{'question':'Сколько реквизитов и табличных в каждом справочнике в системе'})
            self.assertEqual(started['engine'],'ai')
            deadline=time.monotonic()+3
            while self.workspace.ai.active:
                self.assertLess(time.monotonic(),deadline);time.sleep(.01)
        result=self.request('/api/ai/job?id='+started['id'])
        self.assertEqual(result['kind'],'metadata')
        self.assertIn('| Объект |',result['answer'])
        self.assertIn('| Реквизит: количество | Табличная часть: количество |',result['answer'])
        from athena.ai import DEFAULTS
        self.workspace.ai.configure({'provider':'ollama',**DEFAULTS['ollama'],'model':'test'})
        responses=[({'role':'assistant','content':'','tool_calls':[{'id':'configuration','function':{'name':'get_configuration','arguments':'{}'}}]},False,{'input_tokens':1,'output_tokens':1}),
                   ({'role':'assistant','content':'Метаданные прочитаны.'},False,{'input_tokens':1,'output_tokens':1})]
        with patch('athena.code_agent.model_turn',side_effect=responses):
            started=self.request('/api/assistant/ask',{'question':'Объясни структуру текущего проекта'})
            self.assertEqual(started['engine'],'agent')
            deadline=time.monotonic()+3
            while self.workspace.code_agent.active:
                self.assertLess(time.monotonic(),deadline);time.sleep(.01)
        result=self.request('/api/agent/job?id='+started['id'])
        self.assertEqual(result['status'],'ready')
        self.assertEqual(result['mode'],'analyze')
        self.assertIsNone(result['set_id'])
        self.assertEqual(result['steps'][0]['tool'],'get_configuration')

    def test_open_demo_and_fetch_unicode_file(self):
        self.request('/api/open', {'path': '__DEMO__'})
        deadline = time.monotonic() + 5
        while self.request('/api/state')['job']['status'] == 'running':
            if time.monotonic() > deadline:
                self.fail('Loading timed out')
            time.sleep(.01)
        project = self.request('/api/project')
        self.assertEqual(project['name'], 'ДемонстрацияAthena')
        module = next(n for n in project['nodes'] if n['kind'] == 'Module' and 'CommonModules/' in n['source'])
        source = self.request('/api/file?id=' + module['files'][0]['id'])
        self.assertIn('Функция СуммаЗаказа', source['content'])
        self.assertEqual(source['symbols'][0]['name'], 'СуммаЗаказа')
        self.assertEqual(len(self.request('/api/state')['recent']), 1)

    def test_second_launch_opens_existing_instance(self):
        with patch.object(sys, 'argv', ['athena', '--port', str(self.server.server_port)]), patch('athena.server.webbrowser.open') as browser, patch('sys.stdout', new=io.StringIO()):
            main()
            browser.assert_called_once_with(self.url)

    def test_completion_routes_require_auth_and_persist_settings(self):
        for route in ['/api/completion/settings', '/api/completion']:
            with self.assertRaises(HTTPError): self.request(route, {}, authenticated=False)
        result = self.request('/api/completion/settings', {'enabled': False, 'trigger': 'manual', 'shortcut': 'alt-slash', 'mode': 'local'})
        self.assertFalse(result['settings']['enabled'])
        self.assertEqual(self.request('/api/completion/settings')['settings']['shortcut'], 'alt-slash')
        self.assertEqual(self.request('/api/completion', {'before': 'Запрос.'})['items'], [])

    def test_change_sets_routes_scope_persistence_and_prepare(self):
        from athena.catalog import Catalog, read_text, text_hash
        with self.assertRaises(HTTPError): self.request('/api/change-sets', {}, authenticated=False)
        catalog = Catalog(Path(__file__).resolve().parents[1] / 'examples' / 'demo').scan()
        self.workspace.catalog = catalog
        value = self.request('/api/change-sets', {'action':'create','title':'Общий пакет'})
        file_id,path=next((key,path) for key,path in catalog.files.items() if path.suffix=='.bsl')
        before=read_text(path)
        value=self.request('/api/change-sets',{'action':'add','id':value['id'],'revision':value['revision'],'entry':{'kind':'module','file_id':file_id,'base_hash':text_hash(before),'content':before+'\n// Правка\n'}})
        result=self.request('/api/change-sets',{'action':'prepare','id':value['id'],'revision':value['revision']})
        self.assertTrue(result['prepared']);self.assertFalse(result['can_upload'])
        self.assertEqual(self.request('/api/change-sets')['sets'][0]['status'],'prepared')
        review=self.request('/api/change-sets',{'action':'review','id':value['id']})
        self.assertIn('Правка',review['checks'][0]['diff'])

    def test_change_set_upload_flow_checks_result_and_persists_history(self):
        from athena.catalog import Catalog, read_text, text_hash
        catalog=Catalog(Path(__file__).resolve().parents[1]/'examples'/'demo').scan()
        self.workspace.catalog=catalog;self.workspace.active_connection='test-connection'
        profile={'id':'test-connection','name':'Тест','os_auth':True}
        with patch.object(self.workspace.profiles,'all',return_value=[profile]), patch.object(self.workspace.profiles,'save'), patch.object(self.workspace,'index'), patch('athena.server.configuration_changed',return_value=False), patch('athena.change_sets.upload_set') as upload:
            value=self.workspace.change_sets.create('test-connection','Набор')
            file_id,path=next((key,path) for key,path in catalog.files.items() if path.suffix=='.bsl')
            before=read_text(path)
            value=self.workspace.change_sets.add(value['id'],'test-connection',value['revision'],catalog,{'kind':'module','file_id':file_id,'base_hash':text_hash(before),'content':before+'\n// Изменение\n'})
            result=self.workspace.change_set_action({'action':'prepare','id':value['id'],'revision':value['revision']})
            package=result['set']['package']
            with patch('athena.server.export_configuration',return_value=Path(package['folder'])/'files'):
                self.workspace.apply_change({'id':package['id']})
                deadline=time.time()+5
                while self.workspace.job['status']=='running' and time.time()<deadline: time.sleep(.01)
            self.assertEqual(self.workspace.job['status'],'ready',self.workspace.job)
            upload.assert_called_once()
            self.assertEqual(self.workspace.change_sets.read(value['id'])['status'],'applied')

    def test_change_set_failed_write_requires_review_and_cannot_be_reapplied(self):
        from athena.catalog import Catalog, read_text, text_hash
        catalog=Catalog(Path(__file__).resolve().parents[1]/'examples'/'demo').scan()
        self.workspace.catalog=catalog;self.workspace.active_connection='test-connection'
        profile={'id':'test-connection','name':'Тест','os_auth':True}
        with patch.object(self.workspace.profiles,'all',return_value=[profile]), patch('athena.server.configuration_changed',return_value=False), patch('athena.change_sets.upload_set',side_effect=ValueError('Неясный результат')):
            value=self.workspace.change_sets.create('test-connection','Набор')
            file_id,path=next((key,path) for key,path in catalog.files.items() if path.suffix=='.bsl')
            before=read_text(path)
            value=self.workspace.change_sets.add(value['id'],'test-connection',value['revision'],catalog,{'kind':'module','file_id':file_id,'base_hash':text_hash(before),'content':before+'\n// Изменение\n'})
            package=self.workspace.change_set_action({'action':'prepare','id':value['id'],'revision':value['revision']})['set']['package']
            self.workspace.apply_change({'id':package['id']})
            deadline=time.time()+5
            while self.workspace.job['status']=='running' and time.time()<deadline:time.sleep(.01)
            self.assertEqual(self.workspace.change_sets.read(value['id'])['status'],'needs_review')
            with self.assertRaises(ValueError):self.workspace.apply_change({'id':package['id']})

    def test_function_search_and_context_require_authentication(self):
        for route in ['/api/knowledge','/api/search-functions?q=test','/api/function?id=x']:
            with self.assertRaises(HTTPError) as denied:
                self.request(route,authenticated=False)
            self.assertEqual(denied.exception.code,403)
        self.request('/api/open',{'path':'__DEMO__'})
        deadline=time.monotonic()+5
        while self.request('/api/state')['job']['status']=='running':
            if time.monotonic()>deadline:self.fail('Index timed out')
            time.sleep(.01)
        status=self.request('/api/knowledge')
        self.assertTrue(status['ready'])
        self.assertGreater(status['symbols'],0)
        search=self.request('/api/search-functions?q='+quote('СуммаЗаказа'))
        self.assertEqual(search['results'][0]['name'],'СуммаЗаказа')
        detail=self.request('/api/function?id='+search['results'][0]['id'])
        self.assertIn('Функция СуммаЗаказа',detail['code'])
        self.assertTrue(detail['incoming'])

    def test_semantic_settings_auth_index_and_disable(self):
        from test_embeddings import TestProvider
        self.workspace.embedder=TestProvider()
        with self.assertRaises(HTTPError) as denied:self.request('/api/embeddings',{'enabled':True},authenticated=False)
        self.assertEqual(denied.exception.code,403)
        with self.assertRaises(HTTPError):self.request('/api/embeddings',{'enabled':'true'})
        self.request('/api/open',{'path':'__DEMO__'})
        deadline=time.monotonic()+5
        while self.request('/api/state')['job']['status']=='running':
            if time.monotonic()>deadline:self.fail('Index timed out')
            time.sleep(.01)
        fallback=self.request('/api/search-functions?q=test&mode=hybrid')
        self.assertEqual(fallback['mode'],'lexical')
        self.assertTrue(fallback['warning'])
        self.request('/api/embeddings',{'enabled':True})
        while not self.request('/api/knowledge')['semantic_search']:
            if time.monotonic()>deadline:self.fail('Semantic index timed out')
            time.sleep(.01)
        result=self.request('/api/search-functions?q='+quote('стоимость')+'&mode=semantic')
        self.assertEqual(result['results'][0]['name'],'СуммаЗаказа')
        self.assertTrue(json.loads(self.workspace.semantic_config.read_text())['enabled'])
        def unavailable(*args):raise RuntimeError('model unavailable')
        self.workspace.embedder.query=unavailable
        fallback=self.request('/api/search-functions?q='+quote('СуммаЗаказа')+'&mode=hybrid')
        self.assertEqual(fallback['mode'],'lexical')
        self.assertEqual(fallback['results'][0]['name'],'СуммаЗаказа')
        self.assertIn('model unavailable',fallback['warning'])
        with self.assertRaises(HTTPError):self.request('/api/search-functions?q=test&mode=semantic')
        self.request('/api/embeddings',{'enabled':False})
        self.assertFalse(self.request('/api/knowledge')['semantic_search'])
        with self.assertRaises(HTTPError):self.request('/api/search-functions?q=test&mode=semantic')

    def test_export_route_opens_result_and_returns_no_password(self):
        root = Path(self.temp.name)
        base = root / 'base'; base.mkdir(); (base / '1Cv8.1CD').touch()
        exe = root / '1cv8.exe'; exe.touch()
        data = {'name': 'Тестовая база', 'kind': 'file', 'location': str(base), 'platform': str(exe), 'user': 'Test', 'password': 'api-test-secret'}
        demo = Path(__file__).resolve().parent.parent / 'examples/demo'
        def process(args, **kwargs):
            output = Path(args[args.index('/DumpConfigToFiles') + 1])
            shutil.copytree(demo, output, dirs_exist_ok=True)
            Path(args[args.index('/DumpResult') + 1]).write_text('0', encoding='utf-8')
            class Complete:
                returncode = 0
                def poll(self): return 0
            return Complete()
        with patch('athena.exporter.subprocess.Popen', side_effect=process):
            self.assertEqual(self.request('/api/export', data)['status'], 'running')
            deadline = time.monotonic() + 5
            while self.request('/api/state')['job']['status'] == 'running':
                if time.monotonic() > deadline: self.fail('Export timed out')
                time.sleep(.01)
        self.assertEqual(self.request('/api/project')['name'], 'ДемонстрацияAthena')
        connections = self.request('/api/connections')
        self.assertTrue(connections['active'])
        self.assertTrue(connections['profiles'][0]['has_password'])
        self.assertNotIn('api-test-secret', json.dumps(connections))
        with self.assertRaises(HTTPError) as error:
            self.request('/api/cancel', {})
        self.assertEqual(error.exception.code, 400)


if __name__ == '__main__':
    unittest.main()
