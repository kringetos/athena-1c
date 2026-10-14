from pathlib import Path
import sqlite3
import tempfile
import unittest
from athena.bsl_index import extract
from athena.catalog import Catalog
from athena.knowledge import Knowledge

DEMO=Path(__file__).resolve().parent.parent/'examples/demo'

class ParserTests(unittest.TestCase):
    def test_comments_strings_and_multiline_parameters(self):
        text='''// Назначение
&НаСервере
Функция Тест(
    Знач Аргумент = "Функция Ложная()"
) Экспорт
    // Модуль.ЛожныйВызов();
    Результат = "КонецФункции";
    Общий.Метод(Аргумент);
    Возврат Локальная();
КонецФункции'''
        result=extract(text)
        self.assertFalse(result['warnings'])
        self.assertEqual(len(result['symbols']),1)
        sym=result['symbols'][0]
        self.assertEqual(sym['name'],'Тест')
        self.assertEqual(sym['start_line'],1)
        self.assertEqual(sym['declaration_line'],3)
        self.assertEqual(sym['directives'],['&НаСервере'])
        self.assertTrue(sym['export'])
        self.assertEqual([c['name'] for c in sym['calls']],['Метод','Локальная'])
        self.assertEqual(sym['end_line'],10)

    def test_incomplete_method_not_published_as_complete(self):
        result=extract('Процедура Незаконченная()\n А();\nФункция Другая()\n Возврат 1;\nКонецФункции')
        self.assertEqual([s['name'] for s in result['symbols']],['Другая'])
        self.assertTrue(result['warnings'])

class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.store=Knowledge(self.root/'index.sqlite3')
        self.catalog=Catalog(DEMO).scan()
    def tearDown(self):self.temp.cleanup()

    def test_question_search_scopes_document_and_finds_code_without_vectors(self):
        import shutil
        local=self.root/'question';shutil.copytree(DEMO,local)
        path=local/'Documents/ЗаказПокупателя/Ext/ObjectModule.bsl'
        path.write_text(path.read_text(encoding='utf-8')+'\nФункция УстановитьНДС() Экспорт\nВозврат 20; // НДС\nКонецФункции',encoding='utf-8')
        common=local/'CommonModules/РасчётЗаказов/Ext/Module.bsl'
        common.write_text(common.read_text(encoding='utf-8')+'\nФункция НДСДругогоОбъекта()\nВозврат 20;\nКонецФункции',encoding='utf-8')
        snapshot=self.store.index(Catalog(local).scan(),'question')
        rows=self.store.search_context(snapshot,'Найди как задаётся НДС в Заказе покупателя')
        self.assertTrue(rows);self.assertEqual(rows[0]['name'],'УстановитьНДС')
        self.assertTrue(all(r['path'].startswith('Documents/ЗаказПокупателя/') for r in rows))
        self.assertEqual(rows[0]['match'],'object-code')
        document=next(n for n in self.catalog.nodes.values() if n['kind']=='Document')
        document['name']='СчетНаОплату';document['properties']['Synonym']='Счет на оплату'
        alias=self.store.index(self.catalog,'invoice-alias')
        self.assertEqual(self.store.query_terms('Счёте покупателю'),['счет','покупател'])
        self.assertTrue(self.store.search_context(alias,'Найди сумму в счёте покупателю'))
        self.assertEqual(self.store.search_context(snapshot,'НесуществующаяФункцияXYZ'),[])
        other=self.store.index(Catalog(DEMO).scan(),'other');self.assertEqual(self.store.search_context(other,'НДС'),[])

    def test_search_and_context(self):
        snapshot=self.store.index(self.catalog,'demo')
        rows=self.store.search(snapshot,'СуммаЗаказа')
        self.assertEqual(rows[0]['name'],'СуммаЗаказа')
        symbol=self.store.symbol(snapshot,rows[0]['id'])
        self.assertIn('Функция СуммаЗаказа',symbol['code'])
        self.assertGreater(symbol['end_line'],symbol['start_line'])
        self.assertTrue(symbol['incoming'])
        self.assertEqual(symbol['incoming'][0]['resolution'],'candidate')
        self.assertFalse(self.store.status(snapshot)['semantic_search'])

    def test_projects_are_isolated_and_input_is_literal(self):
        first=self.store.index(self.catalog,'first')
        other=self.store.index(self.catalog,'second')
        self.assertNotEqual(first,other)
        sid=self.store.search(first,'СуммаЗаказа')[0]['id']
        with self.assertRaises(ValueError):self.store.symbol(other,sid)
        self.assertIsInstance(self.store.search(first,'" OR * ( --'),list)

    def test_identical_reindex_reuses_snapshot_and_analysis(self):
        first=self.store.index(self.catalog,'demo')
        again=self.store.index(self.catalog,'demo')
        self.assertEqual(first,again)
        with self.store.connect() as db:
            self.assertEqual(db.execute('select count(*) from snapshots').fetchone()[0],1)
            self.assertEqual(db.execute('select count(*) from analyses').fetchone()[0],3)
            self.assertEqual(db.execute('pragma user_version').fetchone()[0],2)

    def test_parser_upgrade_rebuilds_analysis_without_changing_source(self):
        from unittest.mock import patch
        from athena.bsl_index import VERSION
        first=self.store.index(self.catalog,'demo')
        with patch('athena.knowledge.VERSION',VERSION+'-upgrade'):
            second=self.store.index(self.catalog,'demo')
        self.assertNotEqual(first,second)
        self.assertEqual(self.store.search(second,'СуммаЗаказа')[0]['name'],'СуммаЗаказа')
        with self.store.connect() as db:
            self.assertEqual(db.execute('select count(*) from analyses').fetchone()[0],6)
            self.assertEqual(db.execute('select count(*) from blobs').fetchone()[0],3)

    def test_shadowed_receiver_and_invalid_method_do_not_create_links(self):
        import shutil
        local=self.root/'project';shutil.copytree(DEMO,local)
        path=local/'Documents/ЗаказПокупателя/Ext/ObjectModule.bsl'
        path.write_text('''Процедура СТенью(РасчётЗаказов)
    РасчётЗаказов.СуммаЗаказа(Товары);
КонецПроцедуры
Процедура СОшибкой()
    Если Тогда
        РасчётЗаказов.СуммаЗаказа(Товары);
    КонецЕсли;
КонецПроцедуры''',encoding='utf-8')
        snapshot=self.store.index(Catalog(local).scan(),'demo')
        for name in ['СТенью','СОшибкой']:
            method=self.store.symbol(snapshot,self.store.search(snapshot,name)[0]['id'])
            self.assertEqual(method['outgoing'][0]['resolution'],'unresolved')
            self.assertIsNone(method['outgoing'][0]['target_symbol'])
        status=self.store.status(snapshot)
        self.assertGreater(status['warnings'],0)
        self.assertIn('ObjectModule.bsl',status['diagnostics'][0]['path'])

    def test_interrupted_parse_keeps_cache_but_not_partial_snapshot(self):
        from unittest.mock import patch
        def interrupt(phase,done,total):
            if phase.startswith('Разбор BSL') and done==1:raise RuntimeError('interrupted')
        with self.assertRaises(RuntimeError):self.store.index(self.catalog,'demo',interrupt)
        with self.store.connect() as db:
            self.assertEqual(db.execute('select count(*) from analyses').fetchone()[0],1)
            self.assertEqual(db.execute('select count(*) from snapshots').fetchone()[0],0)
        with patch('athena.knowledge.extract',wraps=extract) as parser:
            snapshot=self.store.index(self.catalog,'demo')
        self.assertEqual(parser.call_count,2)
        self.assertTrue(self.store.status(snapshot)['ready'])

    def test_failed_index_keeps_previous_snapshot(self):
        first=self.store.index(self.catalog,'demo')
        import shutil
        local=self.root/'project';shutil.copytree(DEMO,local)
        path=local/'CommonModules/РасчётЗаказов/Ext/Module.bsl'
        path.write_text(path.read_text(encoding='utf-8')+'\n// новая версия',encoding='utf-8')
        def failure(*args):raise RuntimeError('cancelled')
        with self.assertRaises(RuntimeError):self.store.index(Catalog(local).scan(),'demo',failure)
        with self.store.connect() as db:
            self.assertEqual(db.execute('select active_snapshot from projects').fetchone()[0],first)
            self.assertEqual(db.execute('select count(*) from snapshots').fetchone()[0],1)

    def test_changed_function_and_deleted_module_new_version(self):
        import shutil
        local=self.root/'project';shutil.copytree(DEMO,local)
        catalog=Catalog(local).scan();first=self.store.index(catalog,'demo')
        path=local/'CommonModules/РасчётЗаказов/Ext/Module.bsl'
        text=path.read_text(encoding='utf-8').replace('СуммаЗаказа','НоваяСумма')
        path.write_text(text,encoding='utf-8')
        second=self.store.index(Catalog(local).scan(),'demo')
        self.assertNotEqual(first,second)
        self.assertEqual(self.store.search(second,'НоваяСумма')[0]['name'],'НоваяСумма')
        self.assertTrue(self.store.search(first,'СуммаЗаказа'))
        path.unlink()
        third=self.store.index(Catalog(local).scan(),'demo')
        self.assertFalse(self.store.search(third,'НоваяСумма'))
