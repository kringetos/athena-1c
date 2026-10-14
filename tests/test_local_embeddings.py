import tempfile
import unittest
from types import SimpleNamespace
import numpy as np
from athena.embeddings import LocalEmbeddings, settings

class Tokenizer:
    def encode(self, text):
        parts = [SimpleNamespace(ids=list(range(2, n+2)), attention_mask=[1]*n, overflowing=[]) for n in ([3, 5] if 'long' in text else [2])]
        parts[0].overflowing = parts[1:]
        return parts[0]

class Session:
    def __init__(self): self.shapes=[]
    def get_inputs(self): return []
    def run(self, _, feeds):
        ids=feeds['input_ids'];self.shapes.append(ids.shape)
        return [np.stack([ids, ids*ids, np.ones_like(ids)], axis=-1).astype(np.float32)]

class LocalTests(unittest.TestCase):
    def provider(self, batch):
        p=LocalEmbeddings('.', {'batch_size':batch,'token_budget':512,'pause_ms':0})
        p.tokenizer=Tokenizer();p.session=Session()
        return p
    def test_batch_preserves_order_overflow_pooling_and_padding(self):
        single=self.provider(1);batch=self.provider(8)
        a=single.documents(['long', 'short', 'long'])
        b=batch.documents(['long', 'short', 'long'])
        np.testing.assert_allclose(a,b,atol=1e-7)
        self.assertEqual(len(batch.session.shapes),1)
        self.assertEqual(batch.session.shapes[0],(5,5))
        np.testing.assert_allclose(np.linalg.norm(b,axis=1),1)
    def test_validation_and_model_separation(self):
        for config in [{'device':'unknown'}, {'threads':0}, {'token_budget':True}, {'pause_ms':-1}]:
            with self.assertRaises(ValueError): settings(config)
        cpu=self.provider(1)
        self.assertEqual(cpu.model_id,self.provider(8).model_id)
        self.assertNotEqual(cpu.model_id,LocalEmbeddings('.',{'device':'gpu'}).model_id)

    def test_pause_between_model_calls(self):
        p=self.provider(1);p.cancel.set()
        with self.assertRaises(ValueError): p.documents(['long'])
        self.assertEqual(p.session.shapes,[])
        self.assertEqual(len(p.query('short')),3)

    def test_settings_persist_without_start(self):
        from unittest.mock import patch
        from athena.server import Workspace
        from pathlib import Path
        with tempfile.TemporaryDirectory() as root, patch('athena.server.STATE',Path(root)/'recent.json'):
            w=Workspace()
            result=w.set_semantic({'enabled':False,'settings':{'mode':'fast','device':'gpu','threads':1}})
            self.assertFalse(result['semantic']['enabled'])
            self.assertIsNone(w.semantic_worker)
            restored=Workspace()
            self.assertEqual(restored.embedding_settings,w.embedding_settings)
            self.assertEqual(restored.embedder.model_id,w.embedder.model_id)
