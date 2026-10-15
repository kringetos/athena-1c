"""Regression scenarios for delivered evidence, recovery and investigation coverage."""
import base64
import json
import unittest
from unittest.mock import patch
from athena.evidence import read_window, key_fragments
from athena.code_agent import pack_result, model_turn
from athena.agent_memory import ToolMemory
import test_agent_tools as tools_fixture
import test_code_agent as loop_fixture
from test_code_agent import reply


class EvidenceTests(unittest.TestCase):
    def test_transport_continuation_reconstructs_large_module_including_long_line(self):
        text='Начало\n'+('А'*22000)+'\n'+''.join(f'Строка{i}\n' for i in range(500))
        line=1;column=0;parts=[]
        while line is not None:
            raw=read_window(text,line,300,column)
            actual=json.loads(pack_result({'ok':True,'result':raw}))['result']
            self.assertEqual(actual['end_line'],line+len(actual['content'].splitlines())-1)
            parts.append(actual['content'])
            next_line,next_column=actual['next_line'],actual['next_column']
            if next_line is not None:self.assertGreater((next_line,next_column),(line,column))
            line,column=next_line,next_column
        self.assertEqual(''.join(parts),text)

    def test_memory_keeps_body_and_tail_with_hash_and_does_not_mutate_on_compaction(self):
        memory=ToolMemory();text='Процедура Расчёт()\n'+'// заполнитель\n'*220+'Сумма = Цена * Количество;\nКонецПроцедуры'
        response={'ok':True,'result':{'file_id':'file','path':'Module.bsl','hash':'hash',**read_window(text,1,300)}}
        memory.remember('read','read_module',{'file_id':'file'},response)
        before=json.dumps(memory.records,ensure_ascii=False)
        memory.message(1000)
        self.assertEqual(before,json.dumps(memory.records,ensure_ascii=False))
        saved=memory.message(5000)[0]['content']
        self.assertIn('Сумма = Цена * Количество',saved)
        self.assertIn('hash',saved)
        self.assertIn('fragments',saved)

    def test_equivalent_search_and_list_arguments_share_cache(self):
        self.assertEqual(ToolMemory.key('search',{'query':'  Сумма  заказа '}),ToolMemory.key('search',{'query':'сумма заказа','scope':'code','exact':False,'limit':20}))
        self.assertEqual(ToolMemory.key('list_objects',{}),ToolMemory.key('list_objects',{'offset':0,'limit':20}))

    def test_memory_fragments_preserve_tail_and_columns_of_a_long_line(self):
        text='X'*3000+'Сумма = Цена * Количество;'
        fragments=key_fragments(text,8,start_column=4000)
        self.assertIn('Сумма = Цена * Количество;',fragments[-1]['content'])
        self.assertEqual(fragments[-1]['start_line'],8)
        self.assertGreater(fragments[-1]['start_column'],4000)

    def test_native_invalid_argument_shape_reaches_recoverable_validation(self):
        raw={'message':{'content':'','tool_calls':[{'function':{'name':'read_module','arguments':None}}]}}
        with patch('athena.code_agent.request_json',return_value=raw):
            message,_,_=model_turn('ollama',{'base_url':'http://127.0.0.1:11434','model':'test','timeout':30},'',[],[],'ollama',100)
        self.assertEqual(message['tool_calls'][0]['function']['arguments'],'null')


class ToolReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.fixture=tools_fixture.AgentTests();self.fixture.setUp();self.w=self.fixture.w

    def tearDown(self):self.fixture.tearDown()

    def test_agent_search_uses_hybrid_route_and_exact_bypasses_it(self):
        hit=self.w.knowledge.search(self.w.knowledge_snapshot,'СуммаЗаказа')[0]
        with patch.object(self.w,'search_functions',return_value={'mode':'hybrid','warning':'','results':[hit]}) as hybrid:
            result=self.fixture.call('search',{'query':'стоимость покупки'})
            self.assertEqual(result['mode'],'hybrid')
            self.assertEqual(result['groups']['code'][0]['name'],'СуммаЗаказа')
            self.assertIn('code_excerpt',result['groups']['code'][0])
            exact=self.fixture.call('search',{'query':'СуммаЗаказа','exact':True})
            self.assertEqual(exact['mode'],'exact');self.assertEqual(hybrid.call_count,1)

    def test_lexical_fallback_reports_actual_mode(self):
        self.w.semantic_enabled=False
        result=self.fixture.call('search',{'query':'СуммаЗаказа'})
        self.assertEqual(result['mode'],'lexical');self.assertTrue(result['warning'])

    def test_all_scope_without_platform_still_returns_project_code(self):
        result=self.fixture.call('search',{'query':'СуммаЗаказа','scope':'all'})
        self.assertTrue(result['groups']['code'])
        self.assertEqual(result['excluded_scopes'],{'platform':'platform_source_not_selected'})

    def test_register_read_write_and_form_handler_have_actual_code_evidence(self):
        from athena.agent_eval import REGISTER_CODE
        path=self.fixture.file_path
        path.write_text(path.read_text(encoding='utf-8-sig')+REGISTER_CODE,encoding='utf-8')
        self.w.index(self.fixture.root/'project',lambda *a:None)
        for method,expression in [('ПрочитатьОстатки','Набор.Прочитать()'),('ЗаписатьОстатки','Набор.Записать()'),
                                  ('ТоварыКоличествоПриИзменении','СтрокаТоваров.Количество * СтрокаТоваров.Цена')]:
            hit=self.fixture.call('search',{'query':method,'exact':True})['groups']['code'][0]
            result=self.fixture.call('get_symbol',{'symbol_id':hit['id']})['item']
            self.assertIn(expression,result['code'])
            self.assertTrue(result['file_hash']);self.assertGreaterEqual(result['end_line'],result['start_line'])
            calls=self.fixture.call('get_calls',{'symbol_id':hit['id'],'direction':'outgoing'})
            if method!='ТоварыКоличествоПриИзменении':self.assertTrue(calls['items'])

    def test_io_failures_are_distinct_from_invalid_arguments(self):
        with patch.object(self.w.language,'search',side_effect=OSError('temporary')):
            response=self.w.agent_tools.call(self.fixture.request('search',{'query':'Сумма'}))
        self.assertEqual(response['error']['code'],'tool_failure')

    def test_document_can_be_read_to_end_and_is_project_scoped(self):
        project=self.w.agent_tools.context()['project'];kb=self.w.document_knowledge
        text='Стоимость заказа\n'+'Количество * Цена\n'*600
        with patch.object(kb,'schedule'):
            identity=kb.upload(project,{'name':'Расчёт.md','data':base64.b64encode(text.encode()).decode()})['id']
        found=self.fixture.call('search',{'query':'Стоимость','scope':'documents'})
        self.assertIn('index_state',found);self.assertIn('excluded',found)
        parts=[];line=1;column=0
        while line:
            result=self.fixture.call('read_document',{'document_id':identity,'start_line':line,'start_column':column,'line_count':300})
            parts.append(result['content']);line,column=result['next_line'],result['next_column']
        self.assertEqual(''.join(parts),text.strip())
        with self.assertRaises(ValueError):kb.read('other-project',identity)
        kb.configure(project,{'id':identity,'enabled':False,'rules':False})
        result=self.w.agent_tools.call(self.fixture.request('read_document',{'document_id':identity}))
        self.assertFalse(result['ok'])

    def test_large_symbol_returns_delivered_lines_and_module_continuation(self):
        self.fixture.file_path.write_text('Процедура БольшойМетод()\n'+'    Сообщить("данные");\n'*1000+'КонецПроцедуры\n',encoding='utf-8')
        self.w.index(self.fixture.root/'project',lambda *a:None)
        hit=self.fixture.call('search',{'query':'БольшойМетод','exact':True})['groups']['code'][0]
        result=json.loads(pack_result({'ok':True,'result':self.fixture.call('get_symbol',{'symbol_id':hit['id']})}))['result']
        item=result['item']
        self.assertTrue(item['partial']);self.assertLess(item['end_line'],item['symbol_end_line'])
        self.assertEqual(item['next_line'],item['end_line']+1)
        memory=ToolMemory();memory.remember('symbol','get_symbol',{'symbol_id':hit['id']},{'ok':True,'result':result})
        self.assertTrue(memory.coverage()['continuations'])


class LoopReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.fixture=loop_fixture.CodeAgentTests();self.fixture.setUp();self.agent=self.fixture.agent

    def tearDown(self):self.fixture.tearDown()

    def test_three_argument_errors_allow_corrected_call_and_coverage(self):
        responses=[reply('read_module',{'file_id':'x','start_line':False}) for _ in range(3)]
        responses += [reply('read_module',{'file_id':self.fixture.fixture.file_id}),reply(text='Прочитанный модуль подтверждён.')]
        with patch('athena.code_agent.model_turn',side_effect=responses):job=self.fixture.finished(self.fixture.start('analyze'))
        self.assertEqual(job['status'],'ready',job)
        self.assertEqual(job['rounds'],5)
        self.assertEqual(len(job['coverage']['read_ranges']),1)
        self.assertEqual(job['steps'][0]['diagnostics']['outcome'],'invalid_tool_call')

    def test_search_candidates_remain_visible_until_code_is_read(self):
        file=self.fixture.fixture.file_id
        responses=[reply('search',{'query':'СуммаЗаказа'}),reply('read_module',{'file_id':file}),reply(text='Расчёт найден в коде.')]
        with patch('athena.code_agent.model_turn',side_effect=responses):job=self.fixture.finished(self.fixture.start('analyze'))
        self.assertEqual(job['status'],'ready',job)
        self.assertTrue(job['coverage']['candidates'])
        self.assertTrue(job['coverage']['read_ranges'])
        self.assertEqual(job['steps'][0]['diagnostics']['mode'],'lexical')


if __name__=='__main__':unittest.main()
