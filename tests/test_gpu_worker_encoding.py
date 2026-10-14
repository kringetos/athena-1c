import json
import os
import subprocess
import sys
import unittest


class WorkerEncodingTests(unittest.TestCase):
    def test_utf8_protocol_overrides_windows_locale_for_both_pipes(self):
        # Simulate Windows stdio defaults without loading GPU/model dependencies.
        program = '''
import runpy, sys, types
class Vector:
    def __init__(self, text): self.text = text
    def tolist(self): return [len(self.text), ord(self.text[0])]
class Provider:
    def __init__(self, *args): pass
    def _embed(self, texts, prefix):
        assert prefix == 'passage: '
        assert texts == ['НДС в Счёте покупателю 😀']
        return [Vector(text) for text in texts]
sys.modules['athena.embeddings'] = types.SimpleNamespace(LocalEmbeddings=Provider)
runpy.run_module('athena.gpu_worker', run_name='__main__')
'''
        text = 'НДС в Счёте покупателю 😀'
        request = {'models': '.', 'config': {}, 'texts': [text], 'prefix': 'passage: '}
        result = subprocess.run([sys.executable, '-c', program],
                                input=json.dumps(request, ensure_ascii=False) + '\n',
                                capture_output=True, text=True, encoding='utf-8', timeout=20,
                                env={**os.environ, 'PYTHONIOENCODING': 'cp1251:surrogateescape'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'vectors': [[len(text), ord('Н')]]})

    def test_error_response_is_utf8(self):
        program = '''
import runpy, sys, types
class Provider:
    def __init__(self, *args): raise ValueError('Ошибка модели: проверка')
sys.modules['athena.embeddings'] = types.SimpleNamespace(LocalEmbeddings=Provider)
runpy.run_module('athena.gpu_worker', run_name='__main__')
'''
        result = subprocess.run([sys.executable, '-c', program],
                                input=json.dumps({'models': '.', 'config': {}}) + '\n',
                                capture_output=True, text=True, encoding='utf-8', timeout=20,
                                env={**os.environ, 'PYTHONIOENCODING': 'cp1251:surrogateescape'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'error': 'Ошибка модели: проверка'})
