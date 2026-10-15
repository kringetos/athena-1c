import threading
import unittest
from pathlib import Path
from unittest.mock import patch
import xml.etree.ElementTree as ET

from athena.agent_plans import creation_spec
from athena.ai import DEFAULTS
import test_code_agent as loop_fixture


class CreationGrammarTests(unittest.TestCase):
    def test_names_verbs_quotes_and_empty_objects(self):
        for text,kind,object_name in [
            ('Создай справочник Тест без реквизитов','Catalog','Тест'),
            ('Добавьте новый справочник «Поставщики2» без собственных реквизитов.','Catalog','Поставщики2'),
            ('Пожалуйста, создайте документ "Поставка" без дополнительных реквизитов!','Document','Поставка'),
            ('Создать справочник с именем Договоры','Catalog','Договоры'),
            ('добавь пустой документ под названием Order_1','Document','Order_1')]:
            with self.subTest(text=text):
                self.assertEqual(creation_spec(text),{'kind':kind,'name':object_name,'attributes':[],'sections':[]})

    def test_typed_attributes_preserve_names_and_precision(self):
        spec=creation_spec('Создай справочник Партнеры с реквизитами ИНН: Строка(12), Сумма: Число(15,2); Активен: Булево, ДатаСоздания: Дата.')
        self.assertEqual(spec['attributes'],[
            {'name':'ИНН','type':'String','length':12},
            {'name':'Сумма','type':'Number','digits':15,'scale':2},
            {'name':'Активен','type':'Boolean'},{'name':'ДатаСоздания','type':'Date'}])

    def test_natural_attribute_names_and_explicit_type_words(self):
        for text in ['Создай справочник Номенклатура с реквизитом цена',
                     'Создайте новый справочник «Номенклатура» с реквизитом цена.',
                     'Добавь справочник Номенклатура с реквизитом цена типа Число(15,2)']:
            with self.subTest(text=text):
                self.assertEqual(creation_spec(text)['attributes'],[{'name':'цена','type':'Number','digits':15,'scale':2}])
        spec=creation_spec('Создай документ Поставка с реквизитами Товар и Цена, ДатаСоздания, Активен')
        self.assertEqual([a['type'] for a in spec['attributes']],['String','Number','Date','Boolean'])
        self.assertEqual(creation_spec('Создай справочник Партнеры с реквизитом ИНН')['attributes'],[{'name':'ИНН','type':'String'}])

    def test_ambiguous_unsupported_or_additional_requirements_are_not_discarded(self):
        for text in ['Не создавай справочник Тест','Как создать справочник Тест?',
                     'Создай справочник Тест и удали Контрагенты',
                     'Создай справочник Тест без реквизитов, но с формой',
                     'Создай справочник Тест с табличной частью Товары',
                     'Создай справочник Тест с реквизитом ИНН и удали Партнеры',
                     'Создай справочник Тест с реквизитом ИНН: Строка(99999)',
                     'Создай документ Тест с реквизитом Сумма: Число(5,6)',
                     'Создай справочник Тест с реквизитом Активен: Булево(1)',
                     'Создай справочник Тест с реквизитами А: Строка, а: Число',
                     'Создай справочник CON','Создай справочник ../Тест',
                     'Создай справочник «Имя с пробелами»']:
            with self.subTest(text=text):self.assertIsNone(creation_spec(text))


class DirectCreationTests(unittest.TestCase):
    def setUp(self):
        self.fixture=loop_fixture.CodeAgentTests();self.fixture.setUp()
        self.w=self.fixture.w;self.agent=self.fixture.agent

    def tearDown(self):self.fixture.tearDown()

    def run_request(self,goal,**kwargs):
        return self.fixture.finished(self.fixture.start(goal=goal,**kwargs))

    def test_real_tools_prepare_catalog_and_document_without_model_or_remote_permission(self):
        self.w.ai.configure({'provider':'openai',**DEFAULTS['openai'],'base_url':'https://example.com/v1','model':'','allow_remote_code':False})
        before={p.relative_to(self.w.catalog.root):p.read_bytes() for p in self.w.catalog.root.rglob('*') if p.is_file()}
        with patch('athena.code_agent.model_turn',side_effect=AssertionError('Model must not be called')):
            for kind,noun in [('Catalog','справочник'),('Document','документ')]:
                for i in range(3):
                    goal=f'Создай {noun} Проверка{i} без реквизитов'
                    with self.subTest(goal=goal):
                        job=self.run_request(goal)
                        self.assertEqual(job['status'],'ready',job)
                        self.assertEqual(job['stop_reason'],'direct_completed')
                        self.assertEqual(job['rounds'],0)
                        self.assertEqual([s['tool'] for s in job['steps']],['search','create_object','validate_changes','prepare_changes'])
                        value=self.w.change_sets.read(job['set_id'])
                        self.assertEqual(value['status'],'prepared');self.assertEqual(len(value['entries']),1)
                        self.assertEqual(value['entries'][0]['spec']['kind'],kind)
                        selected=value['package']['selected']
                        metadata=f"{'Catalogs' if kind=='Catalog' else 'Documents'}/Проверка{i}.xml"
                        self.assertIn(metadata,selected)
                        root=ET.parse(Path(value['package']['folder'])/'files'/metadata).getroot()
                        self.assertTrue(any(e.tag.endswith('}Name') and e.text==f'Проверка{i}' for e in root.iter()))
        after={p.relative_to(self.w.catalog.root):p.read_bytes() for p in self.w.catalog.root.rglob('*') if p.is_file()}
        self.assertEqual(before,after)

    def test_typed_attributes_reach_the_prepared_package(self):
        with patch('athena.code_agent.model_turn',side_effect=AssertionError('No model')):
            job=self.run_request('Создай справочник Партнеры с реквизитами ИНН: Строка(12), Активен: Булево')
        self.assertEqual(job['status'],'ready',job)
        value=self.w.change_sets.read(job['set_id'])
        self.assertEqual(value['entries'][0]['spec']['attributes'][0]['length'],12)
        metadata=Path(value['package']['folder'])/'files'/'Catalogs'/'Партнеры.xml'
        names=[e.text for e in ET.parse(metadata).getroot().iter() if e.tag.endswith('}Name')]
        self.assertIn('ИНН',names);self.assertIn('Активен',names)

    def test_existing_object_is_not_duplicated(self):
        with patch('athena.code_agent.model_turn',side_effect=AssertionError('No model')):
            job=self.run_request('Создай справочник Номенклатура без реквизитов')
        self.assertEqual(job['status'],'stopped',job)
        self.assertIn('уже существует',job['phase'])
        self.assertEqual(self.w.change_sets.read(job['set_id'])['entries'],[])
        self.assertEqual([s['tool'] for s in job['steps']],['search'])
        self.assertTrue(job['empty_set_archived'])
        self.assertEqual(self.w.change_sets.read(job['set_id'])['status'],'archived')

    def test_user_video_request_prepares_price_without_api_or_model(self):
        # The demo fixture has Nomenclature already; remove it from this isolated
        # fixture to reproduce the user's configuration, where it does not exist.
        path=self.w.catalog.root/'Catalogs/Номенклатура.xml'
        path.unlink()
        config_path=self.w.catalog.root/'Configuration.xml'
        config=ET.parse(config_path)
        from athena.catalog import child
        children=child(child(config.getroot(),'Configuration'),'ChildObjects')
        children.remove(next(n for n in children if (n.text or '').strip()=='Номенклатура'))
        config.write(config_path,encoding='utf-8')
        self.w.index(self.w.catalog.root,lambda *a:None)
        self.w.ai.configure({'provider':'openai',**DEFAULTS['openai'],'base_url':'https://example.com/v1','model':'','allow_remote_code':False})
        with patch('athena.code_agent.model_turn',side_effect=AssertionError('No model')):
            job=self.run_request('Создай справочник Номенклатура с реквизитом цена')
        self.assertEqual(job['status'],'ready',job)
        value=self.w.change_sets.read(job['set_id'])
        self.assertEqual(value['status'],'prepared');self.assertEqual(len(value['entries']),1)
        price=value['entries'][0]['spec']['attributes'][0]
        self.assertEqual((price['name'],price['type'],price['digits'],price['scale']),('цена','Number',15,2))
        self.assertIn('цена — Число(15,2)',job['answer'])
        self.assertNotIn('empty_set_archived',job)

    def test_invalid_standard_attribute_does_not_report_success(self):
        job=self.run_request('Создай справочник Партнеры с реквизитом Ссылка: Строка')
        self.assertEqual(job['status'],'stopped',job)
        self.assertEqual(self.w.change_sets.read(job['set_id'])['entries'],[])
        self.assertFalse(job['steps'][-1]['ok'])

    def test_rules_keep_creation_on_the_model_path(self):
        rule={'id':'rule','name':'Правило','hash':'hash','content':'Не создавай справочники.'}
        with patch.object(self.w.document_knowledge,'rules',return_value=[rule]),patch('athena.code_agent.model_turn',return_value=loop_fixture.reply(text='Создание запрещено правилами.')) as model:
            job=self.run_request('Создай справочник Тест без реквизитов')
        self.assertEqual(model.call_count,1)
        self.assertNotIn('execution',job)
        self.assertEqual(job['completion'],'no_changes');self.assertEqual(job['status'],'stopped')
        self.assertIn(rule['content'],model.call_args.args[3][0]['content'])

    def test_unchanged_results_stop_model_even_when_page_sizes_change(self):
        def model(*args):
            return loop_fixture.reply('list_objects',{'kinds':['Catalog'],'limit':20+len(self.agent.jobs[self.agent.active]['steps'])})
        with patch('athena.code_agent.model_turn',side_effect=model):
            job=self.run_request('Изучи проект и предложи изменения')
        self.assertEqual(job['status'],'stopped',job)
        self.assertEqual(job['stop_reason'],'no_progress')
        self.assertEqual(job['tool_calls'],4)
        self.assertEqual(job['rounds'],4)

    def test_model_claiming_success_with_empty_set_is_not_success(self):
        with patch('athena.code_agent.model_turn',return_value=loop_fixture.reply(text='Готово, всё создано.')):
            job=self.run_request('Изучи проект и предложи новый объект')
        self.assertEqual(job['status'],'stopped');self.assertEqual(job['completion'],'no_changes')
        self.assertIn('изменений не подготовлено',job['answer'])
        self.assertTrue(job['empty_set_archived'])
        value=self.w.change_sets.read(job['set_id'])
        self.assertEqual(value['status'],'archived')
        restored=self.w.change_sets.archive(value['id'],value['project'],value['revision'],restore=True)
        self.assertEqual(restored['status'],'draft')

    def test_provider_failure_archives_empty_set_and_keeps_error(self):
        with patch('athena.code_agent.model_turn',side_effect=ValueError('HTTP 401')):
            job=self.run_request('Изучи проект и предложи изменения')
        self.assertEqual(job['status'],'error')
        self.assertEqual(job['error'],'HTTP 401')
        self.assertTrue(job['empty_set_archived'])
        self.assertEqual(self.w.change_sets.read(job['set_id'])['entries'],[])

    def test_cancellation_during_name_search_prevents_creation(self):
        entered=threading.Event();release=threading.Event()
        original=self.w.agent_tools.call
        def delayed(request):
            if request['tool']=='search':entered.set();release.wait(2)
            return original(request)
        with patch.object(self.w.agent_tools,'call',side_effect=delayed):
            started=self.fixture.start(goal='Создай справочник Тест без реквизитов')
            self.assertTrue(entered.wait(1))
            self.agent.cancel(started['id']);release.set()
            job=self.fixture.finished(started)
        self.assertEqual(job['status'],'cancelled')
        self.assertEqual(self.w.change_sets.read(job['set_id'])['entries'],[])

    def test_project_change_before_creation_prevents_mutation(self):
        original=self.w.agent_tools.call
        def changed(request):
            if request['tool']=='create_object':self.w.revision+=1
            return original(request)
        with patch.object(self.w.agent_tools,'call',side_effect=changed):
            job=self.run_request('Создай справочник Тест без реквизитов')
        self.assertEqual(job['status'],'stopped',job)
        self.assertEqual(self.w.change_sets.read(job['set_id'])['entries'],[])

    def test_prepare_failure_preserves_proposal_without_reporting_success(self):
        original=self.w.agent_tools.call
        def failed(request):
            if request['tool']=='prepare_changes':return {'ok':False,'error':{'code':'tool_failure','message':'Ошибка подготовки пакета'}}
            return original(request)
        with patch.object(self.w.agent_tools,'call',side_effect=failed):
            job=self.run_request('Создай справочник Тест без реквизитов')
        self.assertEqual(job['status'],'stopped',job)
        value=self.w.change_sets.read(job['set_id'])
        self.assertEqual(len(value['entries']),1);self.assertEqual(value['status'],'draft')
        self.assertIsNone(value['package'])
