import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
import numpy as np
from athena.catalog import Catalog
from athena.knowledge import Knowledge, SCHEMA

DEMO=Path(__file__).resolve().parent.parent/'examples/demo'


class TestProvider:
    name='test';provider='test';model='test';version='1';dimensions=3;model_id='test-1'
    def __init__(self):self.inputs=[]
    def documents(self,texts):
        self.inputs.extend(texts)
        return [[1,0,0] if 'Метод: Сумма Заказа.' in text else [0,1,0] for text in texts]
    def query(self,text):return [1,0,0] if text=='стоимость' else [0,1,0]


class EmbeddingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.store=Knowledge(self.root/'knowledge.sqlite3')
        self.catalog=Catalog(DEMO).scan()
        self.snapshot=self.store.index(self.catalog,'demo')
        self.provider=TestProvider()
    def tearDown(self):self.temp.cleanup()

    def test_vectors_search_scope_and_exact_name_priority(self):
        self.store.index_embeddings(self.snapshot,self.provider)
        rows=self.store.semantic_search(self.snapshot,'стоимость',self.provider)
        self.assertEqual([r['name'] for r in rows],['СуммаЗаказа'])
        self.assertAlmostEqual(rows[0]['similarity'],1)
        exact=self.store.hybrid_search(self.snapshot,'СуммаЗаказа',self.provider)
        self.assertEqual(exact[0]['name'],'СуммаЗаказа')
        other=self.store.index(self.catalog,'other-project')
        self.assertFalse(self.store.semantic_status(other,self.provider)['ready'])
        with self.assertRaises(ValueError):self.store.semantic_search(other,'стоимость',self.provider)
        self.assertEqual(self.store.hybrid_search(self.snapshot,'НесуществующаяФункцияXYZ',self.provider),[])

    def test_unchanged_inputs_reuse_vectors_across_snapshots(self):
        self.store.index_embeddings(self.snapshot,self.provider)
        initial=len(self.provider.inputs)
        self.store.index_embeddings(self.snapshot,self.provider)
        self.assertEqual(len(self.provider.inputs),initial)
        local=self.root/'project';shutil.copytree(DEMO,local)
        path=local/'CommonModules/РасчётЗаказов/Ext/Module.bsl'
        path.write_text(path.read_text(encoding='utf-8')+'\n// вне метода',encoding='utf-8')
        changed=self.store.index(Catalog(local).scan(),'demo')
        self.store.index_embeddings(changed,self.provider)
        self.assertEqual(len(self.provider.inputs),initial)
        self.assertTrue(self.store.semantic_status(changed,self.provider)['ready'])
        path.write_text(path.read_text(encoding='utf-8').replace('Итого = 0','Итого = 1'),encoding='utf-8')
        edited=self.store.index(Catalog(local).scan(),'demo')
        self.store.index_embeddings(edited,self.provider)
        self.assertEqual(len(self.provider.inputs),initial+1)
        self.assertTrue(self.store.semantic_status(self.snapshot,self.provider)['ready'])

    def test_different_model_cannot_use_old_vectors(self):
        self.store.index_embeddings(self.snapshot,self.provider)
        other=TestProvider();other.model_id='test-2';other.version='2'
        self.assertFalse(self.store.semantic_status(self.snapshot,other)['ready'])
        self.store.index_embeddings(self.snapshot,other)
        self.assertGreater(len(other.inputs),0)
        with self.store.connect() as db:self.assertEqual(db.execute('select count(*) from embedding_models').fetchone()[0],2)

    def test_failed_batch_does_not_publish_partial_index(self):
        def fail(*args):raise RuntimeError('model failed')
        with self.assertRaises(RuntimeError):self.store.index_embeddings(self.snapshot,self.provider,fail)
        self.assertFalse(self.store.semantic_status(self.snapshot,self.provider)['ready'])
        with self.store.connect() as db:
            self.assertEqual(db.execute('select count(*) from embeddings').fetchone()[0],0)
            self.assertEqual(db.execute('select count(*) from embedding_cache').fetchone()[0],0)
        self.assertEqual(self.store.search(self.snapshot,'СуммаЗаказа')[0]['name'],'СуммаЗаказа')

    def test_checkpoint_survives_pause_and_resume_without_recalculation(self):
        def pause(phase,done,total):
            if done>0:raise RuntimeError('paused')
        with self.assertRaises(RuntimeError):self.store.index_embeddings(self.snapshot,self.provider,pause)
        partial=self.store.semantic_status(self.snapshot,self.provider)
        self.assertFalse(partial['ready']);self.assertEqual(partial['indexed'],partial['total'])
        with self.assertRaises(ValueError):self.store.semantic_search(self.snapshot,'стоимость',self.provider)
        resumed=TestProvider();reopened=Knowledge(self.store.path);reopened.index_embeddings(self.snapshot,resumed)
        self.assertEqual(resumed.inputs,[]);self.assertTrue(self.store.semantic_status(self.snapshot,resumed)['ready'])

    def test_streamed_batches_and_failed_batch_preserve_completed_work(self):
        local=self.root/'many';shutil.copytree(DEMO,local)
        path=local/'CommonModules/РасчётЗаказов/Ext/Module.bsl'
        path.write_text('\n'.join(f'Функция Метод{i}() Экспорт\nВозврат {i};\nКонецФункции' for i in range(45)),encoding='utf-8')
        snapshot=self.store.index(Catalog(local).scan(),'many')
        batches=[]
        def model(texts):
            batches.append(len(texts));self.assertLessEqual(len(texts),16)
            if len(batches)==2:raise RuntimeError('connection lost')
            return [[1,0,0] for _ in texts]
        self.provider.documents=model
        with self.assertRaises(RuntimeError):self.store.index_embeddings(snapshot,self.provider)
        partial=self.store.semantic_status(snapshot,self.provider);self.assertEqual(partial['indexed'],16);self.assertFalse(partial['ready'])
        resumed=TestProvider();self.store.index_embeddings(snapshot,resumed)
        self.assertEqual(len(resumed.inputs),partial['total']-16)
        self.assertTrue(self.store.semantic_status(snapshot,resumed)['ready'])

    def test_invalid_vectors_rejected(self):
        for value in [[0,0,0],[1,2],[float('nan'),1,0],[float('inf'),0,1]]:
            with self.assertRaises(ValueError):self.store.vector_bytes(value,3)
        self.assertAlmostEqual(float(np.linalg.norm(np.frombuffer(self.store.vector_bytes([3,4,0],3),dtype='<f4'))),1)
        self.provider.documents=lambda texts:[[1,2] for _ in texts]
        with self.assertRaises(ValueError):self.store.index_embeddings(self.snapshot,self.provider)
        self.assertFalse(self.store.semantic_status(self.snapshot,self.provider)['ready'])

    def test_corrupt_vector_fails_explicitly(self):
        self.store.index_embeddings(self.snapshot,self.provider)
        with self.store.connect() as db:
            db.execute('update embeddings set vector=?',(b'bad',));db.commit()
        with self.assertRaises(ValueError):self.store.semantic_search(self.snapshot,'стоимость',self.provider)

    def test_v1_database_migrates_preserving_existing_rows(self):
        path=self.root/'old.sqlite3'
        db=sqlite3.connect(path)
        try:
            db.executescript(SCHEMA+'PRAGMA user_version=1;')
            db.execute('insert into projects(id,name) values(?,?)',('existing','Existing'));db.commit()
        finally:db.close()
        migrated=Knowledge(path)
        with migrated.connect() as db:
            self.assertEqual(db.execute('pragma user_version').fetchone()[0],2)
            self.assertEqual(db.execute('select name from projects').fetchone()[0],'Existing')
            self.assertEqual(db.execute('select count(*) from embedding_cache').fetchone()[0],0)
