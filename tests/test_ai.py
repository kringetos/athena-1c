import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from unittest.mock import patch
from athena.ai import AI,DEFAULTS,validate,generate,request_json,context_pack

class Store:
    def symbol(self,snapshot,sid):
        assert snapshot=='snapshot'
        return dict(id=sid,name='Сумма',owner_id='owner',owner_name='Заказ',module_name='Модуль',file_id='file',file_hash='hash',path='Documents/Заказ/Module.bsl',declaration_line=10,start_line=10,end_line=12,parameters='',export=True,code='Функция Сумма()\nВозврат 42;\nКонецФункции',outgoing=[],incoming=[])

def search(question):return dict(snapshot='snapshot',mode='hybrid',results=[{'id':'method'}])

def finished(ai,job):
    deadline=time.monotonic()+3
    while time.monotonic()<deadline:
        result=ai.job(job['id'])
        if result['status']!='running':return result
        time.sleep(.01)
    raise AssertionError('Job timeout')

class AITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.ai=AI(Path(self.temp.name)/'ai.json')
    def tearDown(self):self.ai.close();self.temp.cleanup()
    def configure(self,provider='openai',**kwargs):
        return self.ai.configure(dict(provider=provider,**{**DEFAULTS[provider],**kwargs}))
    def test_separate_profiles_and_memory_only_keys(self):
        self.configure(model='test',api_key='private-key')
        self.configure('ollama',model='qwen')
        state=self.ai.status();self.assertTrue(state['profiles']['openai']['has_api_key'])
        raw=self.ai.path.read_text();self.assertNotIn('private-key',raw);self.assertNotIn('private-key',json.dumps(state))
        restored=AI(self.ai.path);self.assertEqual(restored.status()['profiles']['openai']['model'],'test');self.assertFalse(restored.status()['profiles']['openai']['has_api_key'])
        self.configure(base_url='http://127.0.0.1:9999/v1');self.assertFalse(self.ai.status()['profiles']['openai']['has_api_key'])
    def test_validation(self):
        for url in ['file:///foo','http://remote.example/v1','https://key@host/v1','https://host/v1?key=x']:
            with self.assertRaises(ValueError):self.configure(base_url=url)
        with self.assertRaises(ValueError):self.configure(timeout=True)
        with self.assertRaises(ValueError):self.configure(api_key='bad\nkey')
    def test_context_and_lines_without_model(self):
        result=finished(self.ai,self.ai.start('Сумма',search,Store()))
        self.assertEqual(result['kind'],'context');self.assertEqual(result['sources'][0]['start_line'],10)
        self.assertIn('11: Возврат 42',result['sources'][0]['code'])
    def test_remote_gate_and_context_only(self):
        self.configure(base_url='https://example.com/v1',model='test')
        with self.assertRaises(ValueError):self.ai.start('Сумма',search,Store())
        self.assertEqual(finished(self.ai,self.ai.start('Сумма',search,Store(),True))['kind'],'context')
    def test_answers_and_invalid_citations(self):
        self.configure(model='test')
        with patch('athena.ai.generate',return_value=('Ответ [S1] и [S99]',False)):
            result=finished(self.ai,self.ai.start('Сумма',search,Store()))
        self.assertEqual(result['kind'],'answer');self.assertIn('[S1]',result['answer']);self.assertNotIn('[S99]',result['answer']);self.assertTrue(result['warnings'])
    def test_no_context_no_model_call(self):
        self.configure(model='test')
        with patch('athena.ai.generate') as model:
            result=finished(self.ai,self.ai.start('Сумма',lambda _:dict(snapshot='snapshot',mode='lexical',results=[]),Store()))
        model.assert_not_called();self.assertEqual(result['kind'],'no_context')
    def test_cancel_ignores_late_response(self):
        self.configure(model='test');entered=threading.Event();release=threading.Event()
        def model(*args):entered.set();release.wait(2);return ('Поздний ответ [S1]',False)
        with patch('athena.ai.generate',side_effect=model):
            job=self.ai.start('Сумма',search,Store());self.assertTrue(entered.wait(1));self.ai.cancel(job['id']);release.set()
            deadline=time.monotonic()+2
            while self.ai.active and time.monotonic()<deadline:time.sleep(.01)
        result=self.ai.job(job['id']);self.assertEqual(result['status'],'cancelled');self.assertEqual(result['answer'],'')
    def test_budget_and_chunk_position(self):
        packed=context_pack(Store(),dict(snapshot='snapshot',results=[dict(id='method',chunk_start_line=11)]),4000)
        self.assertEqual(packed[0]['start_line'],11);self.assertLessEqual(sum(len(s['context']) for s in packed),4000)
    def test_actual_http_protocols(self):
        captured=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                data={'models':[{'name':'test'}]} if self.path=='/api/tags' else {'data':[{'id':'test'}]}
                self.send(data)
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])));captured.append((self.path,body,self.headers.get('Authorization')))
                self.send({'message':{'content':'Ответ [S1]'}} if self.path=='/api/chat' else {'choices':[{'message':{'content':'Ответ [S1]'},'finish_reason':'stop'}]})
            def send(self,data):
                raw=json.dumps(data).encode();self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            base=f'http://127.0.0.1:{server.server_port}'
            for provider,url in [('ollama',base),('openai',base+'/v1')]:
                self.configure(provider,base_url=url,model='test',api_key='secret')
                self.assertEqual(self.ai.models()['models'],['test'])
                result=finished(self.ai,self.ai.start('Сумма',search,Store()));self.assertEqual(result['kind'],'answer')
            self.assertEqual([c[0] for c in captured],['/api/chat','/v1/chat/completions'])
            for _,body,key in captured:self.assertFalse(body['stream']);self.assertEqual(key,'Bearer secret');self.assertIn('sources',body['messages'][1]['content'])
        finally:server.shutdown();server.server_close();thread.join()
