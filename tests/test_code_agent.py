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
from athena.code_agent import CodeAgent,model_turn,tool_catalog,protocol_for,settings,pack_result


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

    def test_amount_formula_question_seeds_model_with_verified_code(self):
        self.fixture.file_path.write_text('''Процедура ПересчитатьСумму(СтрокаТовара)
    СтрокаТовара.Сумма = СтрокаТовара.Количество * СтрокаТовара.Цена;
КонецПроцедуры
''',encoding='utf-8')
        self.w.index(self.fixture.root/'project',lambda *args,**kwargs:None)
        context=self.w.agent_tools.context()
        request={'goal':'Найди, где в конфигурации рассчитываются суммы документов. Укажи методы, формулы и строки кода.',
                 'mode':'analyze','context':{k:context[k] for k in ['project','snapshot','workspace_revision']}}
        def model(*args):
            self.assertTrue(any(t['function']['name']=='get_symbol' for t in args[4]))
            self.assertEqual(args[-1],self.agent.config['max_tokens'])
            evidence='\n'.join(message['content'] for message in args[3] if message['role']=='user')
            self.assertIn('ПересчитатьСумму',evidence)
            self.assertIn('СтрокаТовара.Количество * СтрокаТовара.Цена',evidence)
            return reply(text='Расчёт подтверждён в `ПересчитатьСумму`, строка 2.')
        with patch('athena.code_agent.model_turn',side_effect=model):
            job=self.finished(self.agent.start(request))
        self.assertEqual(job['status'],'ready',job)
        self.assertEqual(job['provider'],'ollama')
        self.assertEqual(job['tool_calls'],0)
        self.assertIn('ПересчитатьСумму',job['answer'])
        self.assertEqual(job['formula_sources'][0]['line'],2)

    def test_all_supported_tools_are_exposed_and_new_objects_track_set_revision(self):
        from athena.agent_tools import definitions
        available={d['function']['name'] for d in definitions()}
        self.assertEqual({t['function']['name'] for t in tool_catalog('propose')},available-{'create_change_set'})
        self.assertEqual(len(tool_catalog('analyze')),10)
        spec={'kind':'Catalog','name':'ПоставщикиАгента','attributes':[{'name':'ИНН','type':'String','length':12}]}
        entry='object:Catalog:поставщикиагента'
        responses=[reply('create_object',{'spec':spec}),
                   reply('update_new_object',{'entry_id':entry,'spec':{**spec,'synonym':'Поставщики'}}),
                   reply('validate_changes'),reply('prepare_changes'),reply(text='Предложен новый справочник.')]
        with patch('athena.code_agent.model_turn',side_effect=responses):job=self.finished(self.start())
        self.assertEqual(job['status'],'ready',job)
        self.assertTrue(all(s['ok'] for s in job['steps']),job['steps'])
        value=self.w.change_sets.read(job['set_id'])
        self.assertEqual(value['revision'],job['set_revision'])
        self.assertEqual(value['entries'][0]['spec']['synonym'],'Поставщики')
        self.assertFalse((self.w.catalog.root/'Catalogs'/'ПоставщикиАгента.xml').exists())

    def test_minimal_catalog_can_be_prepared_after_repeated_inventory_with_small_context(self):
        self.w.ai.configure({'provider':'ollama',**DEFAULTS['ollama'],'model':'test-tools','context_chars':18000})
        spec={'kind':'Catalog','name':'Тест','attributes':[],'sections':[]}
        original=(self.w.catalog.root/'Configuration.xml').read_bytes()
        responses=[reply('list_objects',{'kinds':['Catalog']}),
                   reply('list_objects',{'kinds':['Catalog']}),
                   reply('list_objects',{'kinds':['Catalog']}),
                   reply('create_object',{'spec':spec}),reply('validate_changes'),
                   reply('prepare_changes'),reply(text='Справочник Тест подготовлен в предложениях.')]
        histories=[]
        def model(*args):
            histories.append(copy.deepcopy(args[3]))
            return responses.pop(0)
        with patch('athena.code_agent.model_turn',side_effect=model):
            job=self.finished(self.start(goal='Проверь состав конфигурации и создай справочник Тест без собственных реквизитов'))
        self.assertEqual(job['status'],'ready',job)
        self.assertEqual(job['stop_reason'],'model_completed')
        self.assertTrue(all(s['ok'] for s in job['steps']),job['steps'])
        self.assertEqual([s['cached'] for s in job['steps'][:3]],[False,True,True])
        self.assertTrue(any('create_object' in m['content'] and 'Повторные результаты' in m['content']
                            for m in histories[3] if m['role']=='user'))
        value=self.w.change_sets.read(job['set_id'])
        self.assertEqual(value['status'],'prepared')
        self.assertEqual(len(value['entries']),1)
        self.assertEqual({k:value['entries'][0]['spec'][k] for k in spec},spec)
        self.assertTrue(job['validation']['valid'])
        self.assertEqual((self.w.catalog.root/'Configuration.xml').read_bytes(),original)
        self.assertFalse((self.w.catalog.root/'Catalogs'/'Тест.xml').exists())

    def test_context_trimming_never_skips_metadata_page_items(self):
        for next_offset,total in [(100,125),(None,100)]:
            reply_=json.loads(pack_result({'ok':True,'result':{'items':list(range(100)),'total':total,'next_offset':next_offset}}))
            self.assertEqual(len(reply_['result']['items']),20)
            self.assertEqual(reply_['result']['next_offset'],20)
            self.assertTrue(reply_['agent_context_truncated'])

    def test_metadata_facts_survive_eviction_of_whole_chat_group(self):
        self.w.ai.configure({'provider':'ollama',**DEFAULTS['ollama'],'model':'test-tools','context_chars':12000})
        seen=[]
        def model(*args):
            history=args[3];seen.append(copy.deepcopy(history))
            if len(seen)==1:
                message,cut,usage=reply('list_objects',{'kinds':['Catalog']})
                message['content']='Служебный текст. '*1500
                return message,cut,usage
            self.assertFalse(any(m['role']=='tool' for m in history))
            recalled=next(json.loads(m['content']) for m in history if 'athena_verified_observations' in m['content'])
            result=recalled['athena_verified_observations'][0]['result']
            self.assertEqual(result['total'],len([n for n in self.w.catalog.nodes.values() if n['kind']=='Catalog']))
            self.assertTrue(all('child_counts' in n for n in result['items']))
            self.assertIsNone(result['next_offset'])
            return reply(text='Счётчики получены из полного перечня XML.')
        with patch('athena.code_agent.model_turn',side_effect=model):job=self.finished(self.start('analyze'))
        self.assertEqual(job['status'],'ready',job)
        self.assertEqual(job['rounds'],2)
        self.assertGreater(job['history_compactions'],0)
        self.assertEqual(job['memory_facts'],1)

    def test_search_code_evidence_survives_chat_history_eviction(self):
        self.w.ai.configure({'provider':'ollama',**DEFAULTS['ollama'],'model':'test-tools','context_chars':14000})
        seen=[]
        def model(*args):
            seen.append(copy.deepcopy(args[3]))
            if len(seen)==1:return reply('search',{'query':'СуммаЗаказа','scope':'code'},text='Анализ '*4000)
            memory=next(json.loads(m['content']) for m in args[3] if m['role']=='user' and 'athena_verified_observations' in m.get('content',''))
            result=next(r['result'] for r in memory['athena_verified_observations'] if r['tool']=='search')
            hit=result['groups']['code'][0]
            self.assertIn('СтрокаТовара.Сумма',hit['code_excerpt']['content'])
            self.assertTrue(hit['location']['file_id'])
            return reply(text='Подтверждён расчёт суммы по строкам заказа.')
        with patch('athena.code_agent.model_turn',side_effect=model):job=self.finished(self.start('analyze'))
        self.assertEqual(job['status'],'ready',job);self.assertGreater(job['history_compactions'],0)

    def test_slow_search_does_not_freeze_status_cancel_or_project_state(self):
        entered=threading.Event();release=threading.Event()
        def slow(*args,**kwargs):entered.set();release.wait(2);return {'groups':{'code':[]}}
        with patch.object(self.w.language,'search',side_effect=slow),patch('athena.code_agent.model_turn',return_value=reply('search',{'query':'Сумма','scope':'code'})):
            started=self.start('analyze');self.assertTrue(entered.wait(1))
            self.assertTrue(self.w.lock.acquire(timeout=.2));self.w.lock.release()
            self.assertTrue(self.agent.lock.acquire(timeout=.2));self.agent.lock.release()
            self.assertEqual(self.agent.cancel(started['id'])['status'],'cancelled')
            release.set();job=self.finished(started)
        self.assertEqual(job['status'],'cancelled')

    def test_repeated_reads_use_cache_but_keep_tools_until_step_budget(self):
        root=next(n for n in self.w.catalog.nodes.values() if n['kind']=='Configuration')
        histories=[]
        def model(*args):
            histories.append(args[3])
            if not args[4]:return reply(text='Итог по уже прочитанной конфигурации.')
            return reply('get_object',{'object_id':root['id']})
        with patch.object(self.w.agent_tools,'call',wraps=self.w.agent_tools.call) as execute:
            with patch('athena.code_agent.model_turn',side_effect=model):job=self.finished(self.start('analyze'))
        self.assertEqual(job['status'],'ready',job)
        self.assertEqual(execute.call_count,1)
        self.assertEqual(job['rounds'],self.agent.config['max_steps'])
        self.assertEqual([s['cached'] for s in job['steps']],[False]+[True]*(self.agent.config['max_steps']-2))
        self.assertEqual(job['stop_reason'],'step_budget')
        self.assertIn(root['name'],job['steps'][0]['detail'])
        self.assertIn('athena_verified_observations',json.dumps(histories[-1]))

    def test_draft_reads_are_not_reused_after_edit(self):
        identity,path=self.fixture.file_id,self.fixture.file_path
        text=read_text(path);old=text.splitlines()[0];new='// Новая версия\n'+old
        responses=[reply('read_module',{'file_id':identity}),
                   reply('edit_module',{'file_id':identity,'expected_hash':text_hash(text),'edits':[{'old_text':old,'new_text':new}]}),
                   reply('read_module',{'file_id':identity}),reply(text='Проверено.')]
        results=[]
        def model(*args):
            results.extend(json.loads(m['content']) for m in args[3][-1:] if m['role']=='tool' and m.get('name')=='read_module')
            for message in args[3]:
                if message['role']=='user' and 'athena_verified_observations' in message['content']:
                    for record in json.loads(message['content'])['athena_verified_observations']:
                        if record['tool']=='read_module':
                            result=record['result']
                            results.append({'result':{**result,'content':result.get('excerpt','')+'\n'.join(f['content'] for f in result.get('fragments',[]))}})
            return responses.pop(0)
        with patch('athena.code_agent.model_turn',side_effect=model):job=self.finished(self.start())
        self.assertEqual(job['status'],'ready',job)
        self.assertIn('Новая версия',results[-1]['result']['content'])
        self.assertFalse(job['steps'][2]['cached'])

    def test_final_tool_call_is_not_executed_and_verified_data_is_not_lost(self):
        self.agent.configure({**self.agent.config,'max_steps':2})
        with patch('athena.code_agent.model_turn',side_effect=lambda *args:reply('list_objects',{'kinds':['Catalog']})):
            job=self.finished(self.start('analyze'))
        self.assertEqual(job['status'],'stopped')
        self.assertEqual(job['tool_calls'],1)
        self.assertIn('Проверенные данные',job['answer'])
        self.assertIn('| Объект |',job['answer'])
        self.assertNotIn('Предложения',job['phase'])

    def test_final_answer_requests_omit_tools_for_each_transport(self):
        for provider,protocol in [('ollama','ollama'),('openai','chat'),('openai','responses')]:
            raw={'message':{'content':'Итог'}} if protocol=='ollama' else {'choices':[{'message':{'content':'Итог'}}]} if protocol=='chat' else {'output':[{'type':'message','content':[{'type':'output_text','text':'Итог'}]}]}
            with patch('athena.code_agent.request_json',return_value=raw) as request:
                result,_,_=model_turn(provider,{**DEFAULTS[provider],'model':'test'},'',[{'role':'user','content':'Ответь'}],[],protocol,512)
            self.assertEqual(result['content'],'Итог')
            self.assertNotIn('tools',request.call_args.args[1])
            self.assertNotIn('tool_choice',request.call_args.args[1])

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
        self.assertEqual(job['status'],'stopped');self.assertEqual(job['completion'],'invalid_changes')
        self.assertFalse(job['validation']['valid']);self.assertTrue(job['validation']['errors'])

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
