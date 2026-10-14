import copy
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from unittest.mock import patch
import test_agent_tools as fixture_module
from athena.ai import DEFAULTS
from athena.catalog import read_text,text_hash
from athena.code_agent import CodeAgent,model_turn,tool_catalog,protocol_for,settings


def reply(name=None,args=None,text=''):
    message={'role':'assistant','content':text}
    if name:message['tool_calls']=[{'id':'call_'+name,'type':'function','function':{'name':name,'arguments':json.dumps(args or {},ensure_ascii=False)}}]
    return message,False,{'input_tokens':10,'output_tokens':5}


class CodeAgentTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixture_module.AgentTests();self.fixture.setUp();self.w=self.fixture.w;self.agent=self.w.code_agent
        self.w.ai.configure({'provider':'ollama',**DEFAULTS['ollama'],'model':'test-tools'})

    def tearDown(self):
        deadline=time.monotonic()+3
        while self.agent.active and time.monotonic()<deadline:time.sleep(.01)
        self.fixture.tearDown()

    def start(self,mode='propose',**extra):
        c=self.w.agent_tools.context()
        return self.agent.start({'goal':'Добавь поясняющий комментарий к существующему коду','mode':mode,'context':{k:c[k] for k in ['project','snapshot','workspace_revision']}}|extra)

    def finished(self,started):
        deadline=time.monotonic()+3
        while self.agent.active and time.monotonic()<deadline:time.sleep(.01)
        self.assertIsNone(self.agent.active)
        return self.agent.job(started['id'])

    def test_full_edit_validate_prepare_and_persist_without_source_changes(self):
        identity,path=self.fixture.file_id,self.fixture.file_path;original=path.read_bytes();before=read_text(path);hash_=text_hash(before)
        responses=[reply('read_module',{'file_id':identity}),reply('edit_module',{'file_id':identity,'expected_hash':hash_,'edits':[{'old_text':before.splitlines()[0],'new_text':'// Пояснение агента\n'+before.splitlines()[0]}]}),reply('validate_changes'),reply('prepare_changes'),reply(text='Подготовлено предложение. Требуется просмотр пользователем.')]
        histories=[]
        def model(*args):histories.append(copy.deepcopy(args[3]));return responses.pop(0)
        with patch('athena.code_agent.model_turn',side_effect=model):
            job=self.finished(self.start(file_id=identity))
        self.assertEqual(job['status'],'ready');self.assertEqual(job['tool_calls'],4);self.assertTrue(job['validation']['valid'])
        value=self.w.change_sets.read(job['set_id']);self.assertEqual(value['status'],'prepared');self.assertIn('Пояснение агента',value['entries'][0]['content'])
        self.assertEqual(path.read_bytes(),original);self.assertIsNone(self.w.ai.active)
        self.assertIn('hash',json.loads(histories[1][-1]['content'])['result'])
        restored=CodeAgent(self.w,self.fixture.root);self.assertEqual(restored.job(job['id'])['set_id'],job['set_id'])
        self.assertNotIn('tool_calls',json.loads(histories[1][-1]['content']).get('result',{}))

    def test_analyze_mode_never_stages_even_when_model_requests_write(self):
        responses=[reply('edit_module',{'file_id':self.fixture.file_id}),reply(text='Нужен режим предложений.')]
        with patch('athena.code_agent.model_turn',side_effect=responses):job=self.finished(self.start('analyze'))
        self.assertEqual(job['status'],'ready');self.assertIsNone(job['set_id']);self.assertFalse(job['steps'][0]['ok'])
        self.assertEqual(self.w.change_sets.list(str(self.w.catalog.root)),[])
        self.assertFalse(any(t['function']['name']=='edit_module' for t in tool_catalog('analyze')))

    def test_cancel_suppresses_late_tool_calls_and_blocks_second_task(self):
        entered=threading.Event();release=threading.Event()
        def delayed(*args):entered.set();release.wait(2);return reply('edit_module',{'file_id':self.fixture.file_id,'expected_hash':text_hash(read_text(self.fixture.file_path)),'edits':[{'old_text':read_text(self.fixture.file_path).splitlines()[0],'new_text':'// Поздний ответ'}]})
        with patch('athena.code_agent.model_turn',side_effect=delayed):
            started=self.start();self.assertTrue(entered.wait(1));cancelled=self.agent.cancel(started['id']);self.assertEqual(cancelled['status'],'cancelled')
            with self.assertRaises(ValueError):self.start()
            with self.assertRaises(ValueError):self.w.ai.start('Вопрос',lambda x:None,None)
            release.set();job=self.finished(started)
        self.assertEqual(job['status'],'cancelled');self.assertFalse(job['transport_pending']);self.assertEqual(self.w.change_sets.read(job['set_id'])['entries'],[])

    def test_project_change_stops_before_any_tool_execution(self):
        def changed(*args):self.w.revision+=1;return reply('read_module',{'file_id':self.fixture.file_id})
        with patch('athena.code_agent.model_turn',side_effect=changed):job=self.finished(self.start())
        self.assertEqual(job['status'],'stopped');self.assertEqual(job['tool_calls'],0)

    def test_external_set_edit_stops_before_model_write(self):
        def changed(*args):
            job=self.agent.jobs[self.agent.active]
            self.w.change_sets.mutate(job['set_id'],job['context']['project'],job['set_revision'],lambda value:value.update(title='Изменено пользователем'))
            return reply('read_module',{'file_id':self.fixture.file_id})
        with patch('athena.code_agent.model_turn',side_effect=changed):job=self.finished(self.start())
        self.assertEqual(job['status'],'stopped');self.assertEqual(job['tool_calls'],0)

    def test_remote_permission_and_missing_model_do_not_create_sets(self):
        self.w.ai.configure({'provider':'openai',**DEFAULTS['openai'],'base_url':'https://example.com/v1','model':'test','allow_remote_code':False})
        with self.assertRaises(ValueError):self.start()
        self.w.ai.configure({'provider':'ollama',**DEFAULTS['ollama']})
        with self.assertRaises(ValueError):self.start()
        self.assertEqual(self.w.change_sets.list(str(self.w.catalog.root)),[])

    def test_forbidden_set_argument_and_step_limits(self):
        self.agent.configure({**self.agent.config,'max_steps':2})
        with patch('athena.code_agent.model_turn',side_effect=lambda *args:reply('edit_module',{'set_id':'foreign','file_id':self.fixture.file_id,'expected_hash':'x','edits':[]})):
            job=self.finished(self.start())
        self.assertEqual(job['status'],'stopped');self.assertEqual(len(job['steps']),2);self.assertTrue(all(not s['ok'] for s in job['steps']))
        self.assertEqual(self.w.change_sets.read(job['set_id'])['entries'],[])

    def test_invalid_bsl_is_reported_even_if_model_claims_done(self):
        before=read_text(self.fixture.file_path)
        responses=[reply('edit_module',{'file_id':self.fixture.file_id,'expected_hash':text_hash(before),'edits':[{'old_text':before.splitlines()[0],'new_text':'Процедура Незавершенная('}]}),reply(text='Готово')]
        with patch('athena.code_agent.model_turn',side_effect=responses):job=self.finished(self.start())
        self.assertEqual(job['status'],'ready');self.assertFalse(job['validation']['valid']);self.assertTrue(job['validation']['errors'])

    def test_settings_validation_and_interrupted_runs(self):
        with self.assertRaises(ValueError):settings({**self.agent.config,'max_steps':True})
        with self.assertRaises(ValueError):settings({**self.agent.config,'protocol':'other'})
        self.agent.configure({**self.agent.config,'max_steps':4});self.assertEqual(CodeAgent(self.w,self.fixture.root).config['max_steps'],4)
        with patch('athena.code_agent.model_turn',return_value=reply(text='Исследовано')):job=self.finished(self.start('analyze'))
        raw=self.agent.jobs[job['id']];raw['status']='running';self.agent.save(raw)
        self.assertEqual(CodeAgent(self.w,self.fixture.root).job(job['id'])['status'],'interrupted')

    def test_tool_limit_counts_invalid_attempts_and_truncated_reply_never_executes(self):
        self.agent.configure({**self.agent.config,'max_tool_calls':1})
        with patch('athena.code_agent.model_turn',side_effect=lambda *args:reply('forbidden')):job=self.finished(self.start())
        self.assertEqual(job['tool_calls'],1);self.assertEqual(job['status'],'stopped')
        message,_,usage=reply('read_module',{'file_id':self.fixture.file_id})
        with patch('athena.code_agent.model_turn',return_value=(message,True,usage)):job=self.finished(self.start())
        self.assertEqual(job['tool_calls'],0);self.assertEqual(job['status'],'stopped')


class AgentTransportTests(unittest.TestCase):
    def test_native_ollama_chat_and_responses_protocols(self):
        captured=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])));captured.append((self.path,body))
                if self.path.endswith('/responses'):
                    data={'output':[{'type':'reasoning','id':'rs1','summary':[]},{'type':'function_call','call_id':'c1','name':'search','arguments':'{"query":"Сумма"}'}],'usage':{'input_tokens':3,'output_tokens':4}}
                elif self.path=='/api/chat':data={'message':{'content':'','tool_calls':[{'function':{'name':'search','arguments':{'query':'Сумма'}}}],'thinking':'provider reasoning'}}
                else:data={'choices':[{'message':{'content':None,'tool_calls':[{'id':'c1','type':'function','function':{'name':'search','arguments':'{"query":"Сумма"}'}}]},'finish_reason':'tool_calls'}]}
                raw=json.dumps(data).encode();self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            base=f'http://127.0.0.1:{server.server_port}'
            for provider,protocol,url in [('ollama','ollama',base),('openai','chat',base+'/v1'),('openai','responses',base+'/v1')]:
                messages=[{'role':'system','content':'system'},{'role':'user','content':'goal'}]
                assistant,truncated,usage=model_turn(provider,{**DEFAULTS[provider],'model':'test','base_url':url},'',messages,tool_catalog('analyze'),protocol,512)
                self.assertFalse(truncated);self.assertEqual(assistant['tool_calls'][0]['function']['name'],'search')
                result={'role':'tool','name':'search','tool_call_id':assistant['tool_calls'][0]['id'],'content':'{"ok":true}'}
                model_turn(provider,{**DEFAULTS[provider],'model':'test','base_url':url},'',messages+[assistant,result],tool_catalog('analyze'),protocol,512)
            self.assertIn('tool_name',captured[1][1]['messages'][-1]);self.assertIn('tool_call_id',captured[3][1]['messages'][-1])
            self.assertEqual(captured[5][1]['input'][-1]['type'],'function_call_output');self.assertEqual(captured[5][1]['input'][2]['type'],'reasoning')
            self.assertFalse(captured[5][1]['store'])
        finally:server.shutdown();server.server_close();thread.join()

    def test_auto_protocol_preserves_custom_provider(self):
        self.assertEqual(protocol_for('openai',{'base_url':'https://api.openai.com/v1'},'auto'),'responses')
        self.assertEqual(protocol_for('openai',{'base_url':'http://127.0.0.1:1234/v1'},'auto'),'chat')
