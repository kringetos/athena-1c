import json
import tempfile
import unittest
from pathlib import Path
from athena.catalog import Catalog
from athena.knowledge import Knowledge
from athena.reference import Reference
from athena.language import LanguageKnowledge


class LanguageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.reference = Reference(self.root / 'reference.sqlite3')
        with self.reference.connect() as db:
            db.execute('INSERT INTO sources VALUES(?,?,?,?,?)', ('platform1','8.5','bin','hash1',2))
            db.execute('INSERT INTO sources VALUES(?,?,?,?,?)', ('platform2','8.3','bin2','hash2',1))
            for source, page in [('platform1','p1'), ('platform2','p2')]:
                sections = {'Синтаксис':'Выполнить()', 'Возвращаемое значение':'Тип: РезультатЗапроса.', 'Доступность':'Сервер'}
                db.execute('INSERT INTO pages VALUES(?,?,?,?,?,?,?)', (page,source,'shcntx_ru.hbk','Query/methods/Execute.html','Запрос.Выполнить (Query.Execute)','Выполняет запрос',json.dumps(sections)))
                db.execute('INSERT INTO page_search VALUES(?,?,?,?)', (page,source,'Запрос.Выполнить','Выполняет запрос'))
            db.commit()
        self.knowledge = Knowledge(self.root / 'knowledge.sqlite3')
        self.snapshot = self.knowledge.index(Catalog(Path(__file__).resolve().parents[1] / 'examples' / 'demo').scan(), 'demo')
        self.language = LanguageKnowledge(self.reference, self.knowledge.path)

    def tearDown(self): self.temp.cleanup()

    def test_platform_exact_alias_signatures_members_and_version_isolation(self):
        for query in ['Запрос.Выполнить','Выполнить','Query.Execute','Execute']:
            row = self.language.search(query,'platform1',scope='platform',exact=True)['groups']['platform'][0]
            self.assertEqual(row['id'],'p1'); self.assertEqual(row['signature'],'Выполнить()')
        self.assertEqual(self.language.members('Запрос','platform1')[0]['id'],'p1')
        self.assertEqual(self.language.item('platform','p2','platform2')['availability'],'Сервер')
        with self.assertRaises(ValueError): self.language.item('platform','p2','platform1')
        self.assertEqual(self.language.search('НесуществующийМетод','platform1',scope='platform',exact=True)['groups']['platform'],[])

    def test_project_search_and_method_card_include_provenance_and_calls(self):
        result = self.language.search('СуммаЗаказа','platform1',self.snapshot,exact=True)
        row = result['groups']['code'][0]
        self.assertIn('СуммаЗаказа(',row['signature'])
        card = self.language.item('code',row['id'],snapshot=self.snapshot)
        self.assertIn('Функция',card['body']); self.assertIn('incoming',card); self.assertIn('outgoing',card)
        self.assertEqual(card['location']['snapshot'],self.snapshot)
        with self.knowledge.connect() as db:
            name = json.loads(db.execute("SELECT data FROM metadata WHERE snapshot_id=? AND json_extract(data,'$.kind')='Catalog' LIMIT 1",(self.snapshot,)).fetchone()[0])['name']
        rows = self.language.search(name,snapshot=self.snapshot,scope='metadata',exact=True)['groups']['metadata']
        self.assertTrue(rows)
        card = self.language.item('metadata',rows[0]['id'],snapshot=self.snapshot)
        self.assertEqual(card['node']['name'],name); self.assertIn('children',card)

    def test_invalid_scope_snapshot_and_source_are_rejected(self):
        with self.assertRaises(ValueError): self.language.search('Запрос','missing',scope='platform')
        with self.assertRaises(ValueError): self.language.search('Запрос',snapshot='missing',scope='code')
        with self.assertRaises(ValueError): self.language.search('Запрос',scope='code')
        with self.assertRaises(ValueError): self.language.search('Запрос',scope='unknown')

    def test_reference_change_refreshes_derived_symbols(self):
        self.language.ensure_platform('platform1')
        with self.reference.connect() as db:
            db.execute("UPDATE sources SET fingerprint='updated' WHERE id='platform1'")
            db.execute("UPDATE pages SET title='Запрос.Проверить (Query.Check)' WHERE id='p1'"); db.commit()
        self.assertEqual(self.language.members('Запрос','platform1')[0]['name'],'Запрос.Проверить')

    def test_read_only_project_reader_cannot_write(self):
        import sqlite3
        with self.language.reader.connect() as db:
            with self.assertRaises(sqlite3.OperationalError): db.execute('DELETE FROM projects')
