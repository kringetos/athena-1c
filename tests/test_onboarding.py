import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from athena.server import Workspace

class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.patch=patch('athena.server.STATE',self.root/'recent.json');self.patch.start()
        self.workspace=Workspace()
    def tearDown(self):
        self.workspace.close();self.patch.stop();self.temp.cleanup()
    def test_initial_state_and_persisted_completion(self):
        state=self.workspace.onboarding_status()
        self.assertFalse(state['completed'])
        self.assertGreaterEqual(state['hardware']['cpu_threads'],1)
        result=self.workspace.complete_onboarding({'name':'Анна','settings':{'threads':1}})
        self.assertEqual(result['name'],'Анна')
        restored=Workspace()
        self.assertTrue(restored.onboarding_status()['completed'])
        self.assertFalse(restored.semantic_enabled)
        self.assertIsNone(restored.semantic_worker)
        self.assertEqual(restored.embedding_settings['threads'],1)
        restored.close()
    def test_invalid_ai_does_not_change_settings_or_complete(self):
        before=dict(self.workspace.embedding_settings)
        with self.assertRaises(ValueError):
            self.workspace.complete_onboarding({'settings':{'mode':'fast'},'ai':{'provider':'bad'}})
        self.assertEqual(before,self.workspace.embedding_settings)
        self.assertFalse((self.root/'user.json').exists())
    def test_ai_key_only_in_memory(self):
        self.workspace.complete_onboarding({'ai':{'provider':'openai','base_url':'http://127.0.0.1:1234/v1','model':'local','api_key':'test-secret'}})
        self.assertTrue(self.workspace.ai.status()['profiles']['openai']['has_api_key'])
        self.assertNotIn('test-secret',(self.root/'ai.json').read_text())
        self.assertNotIn('test-secret',(self.root/'user.json').read_text())
    def test_invalid_profile(self):
        for name in [12,'x'*81,'a\nb']:
            with self.assertRaises(ValueError):self.workspace.complete_onboarding({'name':name})

    def test_http_setup_page_and_authorized_completion(self):
        import re
        import threading
        from urllib.request import urlopen, Request
        from urllib.error import HTTPError
        from athena.server import LocalServer,handler
        server=LocalServer(('127.0.0.1',0),handler(self.workspace))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:'+str(server.server_port)
        try:
            html=urlopen(base+'/setup').read().decode()
            self.assertIn('Первоначальная настройка',html)
            token=re.search(r'name="athena-token" content="([^"]+)"',html).group(1)
            with self.assertRaises(HTTPError):urlopen(base+'/api/onboarding')
            body=json.dumps({'name':'Тест','settings':{'threads':1}}).encode()
            request=Request(base+'/api/onboarding',data=body,headers={'X-Athena-Token':token,'Content-Type':'application/json'})
            self.assertTrue(json.load(urlopen(request))['completed'])
            self.assertFalse(self.workspace.semantic_enabled)
            self.assertIsNone(self.workspace.semantic_worker)
        finally:
            server.shutdown();server.server_close();thread.join()

    def test_platform_saved_and_used_as_export_default(self):
        exe=self.root/'bin'/'1cv8.exe';exe.parent.mkdir();exe.touch()
        self.workspace.complete_onboarding({'platform':str(exe)})
        self.assertEqual(self.workspace.onboarding_status()['platform'],str(exe.resolve()))
        self.assertEqual(self.workspace.connections()['preferred_platform'],str(exe.resolve()))
        with self.assertRaises(ValueError):self.workspace.complete_onboarding({'platform':str(self.root/'1cv8c.exe')})
    def test_connection_probe_does_not_save_settings_or_key(self):
        before=self.workspace.ai.status()
        with patch('athena.ai.request_json',return_value={'data':[{'id':'test-model'}]}) as request:
            result=self.workspace.ai.test_connection({'provider':'openai','base_url':'http://127.0.0.1:1234/v1','model':'','api_key':'temporary-secret'})
        self.assertEqual(result['models'],['test-model'])
        self.assertEqual(self.workspace.ai.status(),before)
        self.assertFalse((self.root/'ai.json').exists())
        self.assertEqual(request.call_args.args[0],'http://127.0.0.1:1234/v1/models')
        self.assertEqual(request.call_args.kwargs['key'],'temporary-secret')

    def test_saved_nonstandard_platform_is_discovered(self):
        exe=self.root/'custom-install'/'1cv8.exe';exe.parent.mkdir();exe.touch()
        with patch('athena.server.platforms',return_value=[]),patch.object(self.workspace.profiles,'all',return_value=[{'platform':str(exe)}]):
            self.assertIn(str(exe.resolve()),self.workspace.onboarding_status()['platforms'])
