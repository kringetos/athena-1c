import shutil
import tempfile
import unittest
from pathlib import Path

from athena.catalog import Catalog, read_text, symbols

DEMO = Path(__file__).resolve().parent.parent / 'examples' / 'demo'


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'export'
        shutil.copytree(DEMO, self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_nested_forms_and_modules_belong_to_correct_objects(self):
        catalog = Catalog(self.root).scan()
        self.assertFalse(catalog.warnings)
        nodes = list(catalog.nodes.values())
        order = next(n for n in nodes if n['kind'] == 'Document')
        forms = [n for n in nodes if n['kind'] == 'Form']
        self.assertEqual(len(forms), 1)
        form = forms[0]
        self.assertEqual(form['parent'], order['id'])
        self.assertTrue(any(f['path'].endswith('Form/Module.bsl') for f in form['files']))
        section = next(n for n in nodes if n['kind'] == 'TabularSection')
        self.assertEqual(len(section['children']), 4)
        self.assertEqual(catalog.summary()['stats']['modules'], 3)
        self.assertEqual(len(catalog.file(form['files'][0]['id'])['symbols']), 2)

    def test_missing_object_is_preserved_and_bad_xml_reported(self):
        (self.root / 'Catalogs' / 'Номенклатура.xml').unlink()
        (self.root / 'Documents' / 'Сломанный.xml').write_text('<MetaDataObject><broken>', encoding='utf-8')
        catalog = Catalog(self.root).scan()
        missing = next(n for n in catalog.nodes.values() if n['name'] == 'Номенклатура' and n['kind'] == 'Catalog')
        self.assertTrue(missing['missing'])
        self.assertEqual(len(catalog.warnings), 2)

    def test_refresh_detects_added_and_deleted_files(self):
        first = Catalog(self.root).scan()
        path = self.root / 'Documents' / 'ЗаказПокупателя' / 'Ext' / 'ManagerModule.bsl'
        path.write_text('Процедура НовыйМетод()\nКонецПроцедуры', encoding='utf-8')
        second = Catalog(self.root).scan()
        self.assertEqual(second.summary()['stats']['modules'], first.summary()['stats']['modules'] + 1)
        path.unlink()
        self.assertEqual(Catalog(self.root).scan().summary()['stats'], first.summary()['stats'])

    def test_root_required_and_unknown_file_rejected(self):
        with self.assertRaises(ValueError):
            Catalog(self.root / 'Documents').scan()
        with self.assertRaises(ValueError):
            Catalog(self.root).scan().file('../../outside')

    def test_auxiliary_xml_not_treated_as_metadata(self):
        path = self.root / 'Documents' / 'ЗаказПокупателя' / 'Forms' / 'ФормаДокумента' / 'Ext' / 'Form.xml'
        path.write_text('<Form><Attributes><Attribute name="Объект"/></Attributes></Form>', encoding='utf-8')
        catalog = Catalog(self.root).scan()
        self.assertEqual(sum(n['kind'] == 'Form' for n in catalog.nodes.values()), 1)

    def test_separate_definitions_with_equal_names_keep_their_file_owners(self):
        # A partial/inconsistent export can contain several definitions with the same name.
        # Never delete a separate definition while resolving embedded references.
        directory = self.root / 'CommonModules'
        for filename in ['Первый', 'Второй']:
            (directory / (filename + '.xml')).write_text('<MetaDataObject><CommonModule><Properties><Name>ПовторяющеесяИмя</Name></Properties></CommonModule></MetaDataObject>', encoding='utf-8')
        catalog = Catalog(self.root).scan()
        nodes = [n for n in catalog.nodes.values() if n['name'] == 'ПовторяющеесяИмя']
        self.assertEqual(len(nodes), 2)
        self.assertTrue(all(n['files'] for n in nodes))

    def test_encoding_and_navigation_ignore_strings_and_comments(self):
        text = '// Процедура Ложная()\nФункция Настоящая() Экспорт\nТекст = "Первая строка\n|Процедура ИзСтроки()\n|Последняя строка";\nКонецФункции\nAsync Procedure Second()\nEndProcedure'
        self.assertEqual([s['name'] for s in symbols(text)], ['Настоящая', 'Second'])
        self.assertEqual([s['line'] for s in symbols(text)], [2, 7])
        for encoding in ['utf-8-sig', 'utf-16', 'cp1251']:
            path = self.root / 'encoding.bsl'
            path.write_bytes('Процедура Проверка()'.encode(encoding))
            self.assertEqual(read_text(path), 'Процедура Проверка()')


if __name__ == '__main__':
    unittest.main()
