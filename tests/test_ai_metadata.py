import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from athena.ai import AI, DEFAULTS
from athena.ai_metadata import inventory_request, inventory_answer, structure_request
from athena.catalog import Catalog
from athena.knowledge import Knowledge


DEMO = Path(__file__).resolve().parents[1] / 'examples/demo'


class MetadataAnswerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Knowledge(self.root / 'knowledge.sqlite3')
        self.catalog = Catalog(DEMO).scan()
        self.configuration = next(n['id'] for n in self.catalog.nodes.values() if n['kind'] == 'Configuration')
        self.catalog.add('Document/БезКода', 'БезКода', 'Document', self.configuration,
                         source='Documents/БезКода.xml')
        self.catalog.add('missing/Document/БезВыгрузки', 'БезВыгрузки', 'Document',
                         self.configuration, missing=True)
        self.snapshot = self.store.index(self.catalog, 'metadata-test')
        self.ai = AI(self.root / 'ai.json')

    def tearDown(self):
        self.ai.close()
        self.temp.cleanup()

    def retrieve(self, question):
        return {'snapshot': self.snapshot, 'mode': 'metadata', 'results': [],
                'inventory': self.store.metadata_inventory(self.snapshot, inventory_request(question))}

    def finished(self, job):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = self.ai.job(job['id'])
            if result['status'] != 'running':
                return result
            time.sleep(.01)
        self.fail('Metadata answer did not finish')

    def test_inventory_includes_objects_without_code_and_missing_xml(self):
        inventory = self.store.metadata_inventory(self.snapshot, ['Document'])
        expected = {n['name'] for n in self.catalog.nodes.values() if n['kind'] == 'Document'}
        self.assertEqual({n['name'] for n in inventory['objects']}, expected)
        self.assertIn('БезКода', expected)
        self.assertIn('БезВыгрузки', expected)
        self.assertTrue(inventory['complete'])
        answer, sources = inventory_answer(inventory)
        self.assertIn('Документы: ' + str(len(expected)), answer)
        self.assertEqual(len(sources), len(expected))
        missing = next(s for s in sources if s['name'] == 'БезВыгрузки')
        self.assertIn('XML отсутствует', missing['code'])
        self.assertTrue(all(s['object_id'] in self.catalog.nodes for s in sources))

    def test_inventory_uses_requested_snapshot(self):
        self.catalog.add('Document/Поздний', 'Поздний', 'Document', self.configuration)
        other = self.store.index(self.catalog, 'other-project')
        old = self.store.metadata_inventory(self.snapshot, ['Document'])
        new = self.store.metadata_inventory(other, ['Document'])
        self.assertNotIn('Поздний', [n['name'] for n in old['objects']])
        self.assertIn('Поздний', [n['name'] for n in new['objects']])
        with self.assertRaises(ValueError):
            self.store.metadata_inventory('missing-snapshot', ['Document'])

    def test_truncated_list_never_claims_completeness(self):
        inventory = self.store.metadata_inventory(self.snapshot, ['Document'], limit=1)
        self.assertFalse(inventory['complete'])
        answer, sources = inventory_answer(inventory)
        self.assertEqual(len(sources), 1)
        self.assertIn('Показаны первые 1 из ' + str(inventory['total']), answer)

    def test_empty_and_multiple_categories(self):
        empty = self.store.metadata_inventory(self.snapshot, ['AccountingRegister'])
        self.assertEqual(empty['total'], 0)
        answer, sources = inventory_answer(empty)
        self.assertIn('не обнаружены', answer)
        self.assertEqual(sources, [])
        kinds = ['Document', 'Catalog']
        both = self.store.metadata_inventory(self.snapshot, kinds)
        self.assertEqual({n['kind'] for n in both['objects']}, set(kinds))
        all_objects = self.store.metadata_inventory(self.snapshot, [])
        self.assertNotIn('Module', {n['kind'] for n in all_objects['objects']})

    def test_metadata_answer_never_calls_llm_even_when_remote_code_is_blocked(self):
        self.ai.configure(dict(DEFAULTS['openai'], provider='openai',
                               base_url='https://example.com/v1', model='test'))
        with patch('athena.ai.generate') as model:
            result = self.finished(self.ai.start('Какие есть документы?', self.retrieve, self.store))
        model.assert_not_called()
        self.assertEqual(result['kind'], 'metadata')
        self.assertTrue(result['inventory_complete'])
        self.assertIn('БезКода', result['answer'])
        self.assertEqual(result['snapshot'], self.snapshot)
        self.assertTrue(all(s['type'] == 'metadata' for s in result['sources']))
        self.assertTrue(all(s['snapshot'] == self.snapshot for s in result['sources']))

    def test_context_only_is_also_snapshot_metadata_without_a_model(self):
        result = self.finished(self.ai.start('Список справочников', self.retrieve, self.store, True))
        self.assertEqual(result['kind'], 'context')
        self.assertEqual(result['mode'], 'metadata')
        self.assertTrue(result['inventory_complete'])

    def test_category_routing_does_not_swallow_code_or_object_questions(self):
        for question, kinds in [
            ('Какие есть документы?', ['Document']),
            ('Сколько документов в конфигурации?', ['Document']),
            ('Перечисли все документы', ['Document']),
            ('Документы', ['Document']),
            ('Покажи справочники', ['Catalog']),
            ('Какие общие модули есть?', ['CommonModule']),
            ('Какие регистры сведений есть?', ['InformationRegister']),
            ('Какие есть документы и справочники?', ['Document', 'Catalog']),
            ('Какой состав конфигурации?', []),
        ]:
            with self.subTest(question=question):
                self.assertEqual(inventory_request(question), kinds)
        for question in ['Как проводится документ Материалы?', 'Покажи код документа',
                         'Какие реквизиты у документа Материалы?',
                         'Сколько табличных частей у справочника Материалы?',
                         'Какие документы вызывают функцию Проверка?',
                         'Покажи документ Материалы', 'Документ Материалы',
                         'Какие документы связаны с платежами?', 'Как работает метод?']:
            with self.subTest(question=question):
                self.assertIsNone(inventory_request(question))

    def test_attribute_counts_include_zero_missing_and_exclude_section_attributes(self):
        parent=self.catalog.add('Catalog/Проверка','Проверка','Catalog',self.configuration)
        self.catalog.add('Catalog/Проверка/Attribute','ИНН','Attribute',parent)
        section=self.catalog.add('Catalog/Проверка/Section','Строки','TabularSection',parent)
        self.catalog.add('Catalog/Проверка/Section/Attribute','Цена','Attribute',section)
        self.catalog.add('Catalog/Проверка/Standard','Код','StandardAttribute',parent)
        self.catalog.add('Catalog/Пустой','Пустой','Catalog',self.configuration)
        self.catalog.add('Catalog/НетXML','НетXML','Catalog',self.configuration,missing=True)
        snapshot=self.store.index(self.catalog,'counts')
        inventory=self.store.metadata_inventory(snapshot,['Catalog'])
        inventory['field']='Attribute'
        by_name={n['name']:n for n in inventory['objects']}
        self.assertEqual(by_name['Проверка']['child_counts']['Attribute'],1)
        self.assertEqual(by_name['Пустой']['child_counts'],{})
        self.assertIsNone(by_name['НетXML']['child_counts'])
        answer,sources=inventory_answer(inventory)
        self.assertIn('| Проверка | 1 |',answer)
        self.assertIn('| Пустой | 0 |',answer)
        self.assertIn('| НетXML | XML отсутствует |',answer)
        self.assertEqual(len(sources),inventory['total'])

    def test_structural_question_routing_handles_user_typo_and_named_object(self):
        self.assertEqual(structure_request('Сколько реквизитом в каждом справочнике в системе'),{'kinds':['Catalog'],'field':'Attribute'})
        self.assertEqual(structure_request('Сколько табличных частей во всех документах?'),{'kinds':['Document'],'field':'TabularSection'})
        self.assertIsNone(structure_request('Сколько реквизитов у справочника Материалы в конфигурации?'))
        self.assertIsNone(structure_request('Сколько стандартных реквизитов в каждом справочнике?'))
        self.assertIsNone(structure_request('Сколько реквизитов табличных частей в каждом справочнике?'))
        self.assertIsNone(structure_request('Сколько числовых реквизитов в каждом справочнике?'))

    def test_combined_attribute_and_section_question_counts_both_without_code(self):
        request=structure_request('Сколько реквизитов и табличных в каждом справочнике в системе')
        self.assertEqual(request,{'kinds':['Catalog'],'fields':['Attribute','TabularSection']})
        inventory=self.store.metadata_inventory(self.snapshot,request['kinds'])
        inventory['fields']=request['fields']
        answer,sources=inventory_answer(inventory)
        self.assertIn('| Реквизит: количество | Табличная часть: количество |',answer)
        for node in inventory['objects']:
            counts=node['child_counts']
            self.assertIn(f"| {node['name']} | {counts.get('Attribute',0)} | {counts.get('TabularSection',0)} |",answer)
