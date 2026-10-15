import json
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import Request,urlopen
from urllib.error import HTTPError
import test_agent_tools as fixtures
from athena.accounts import ACTOR
from athena.server import LocalServer,handler
from athena.ai import DEFAULTS
from athena.catalog import read_text,text_hash

class AccountTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.AgentTests();self.fixture.setUp();self.w=self.fixture.w;self.accounts=self.w.accounts
        self.admin_token=self.accounts.login({'login':'admin','password':'admin'})
        actor=self.accounts.session(self.admin_token);mark=ACTOR.set(actor)
        try:
            self.accounts.create({'login':'developer','password':'test123','role':'programmer'})
            self.accounts.create({'login':'leader','password':'test123','role':'lead'})
            self.accounts.create({'login':'otherdev','password':'test123','role':'programmer'})
        finally:ACTOR.reset(mark)
        self.tokens={'admin':self.admin_token}
        for login in ('developer','leader','otherdev'):self.tokens[login]=self.accounts.login({'login':login,'password':'test123'})
        self.server=LocalServer(('127.0.0.1',0),handler(self.w));self.worker=threading.Thread(target=self.server.serve_forever,daemon=True);self.worker.start()
        self.url='http://127.0.0.1:'+str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.worker.join();self.fixture.tearDown()

    def request(self,path,body=None,user='admin',cookie=True):
        headers={'X-Athena-Token':self.w.token,'Content-Type':'application/json'}
        if cookie:headers['Cookie']='athena_session='+self.tokens[user]
        with urlopen(Request(self.url+'/api/'+path,data=json.dumps(body).encode() if body is not None else None,headers=headers),timeout=5) as r:return json.load(r)

    def test_sessions_server_permissions_and_password_storage(self):
        with self.assertRaises(HTTPError) as denied:self.request('state',cookie=False)
        self.assertEqual(denied.exception.code,403)
        for user in ('developer','leader'):
            with self.assertRaises(HTTPError) as denied:self.request('auth/users',{'login':'hacker','password':'test','role':'admin'},user)
            self.assertEqual(denied.exception.code,403)
        with self.assertRaises(HTTPError):self.request('review/requests',user='developer')
        self.assertEqual(self.request('review/requests',user='leader')['items'],[])
        self.assertEqual(self.request('auth/me',user='developer')['role'],'programmer')
        with self.accounts.db() as db:
            row=db.execute("SELECT password,salt FROM users WHERE login='admin'").fetchone()
            self.assertNotEqual(row['password'],b'admin');self.assertEqual(len(row['salt']),16)
        self.request('auth/logout',{},'developer')
        with self.assertRaises(HTTPError):self.request('state',user='developer')

    def test_real_transport_audit_actor_survives_agent_thread_and_feedback_indexes(self):
        self.w.ai.configure({'provider':'ollama',**DEFAULTS['ollama'],'model':'test'})
        c=self.w.agent_tools.context();context={k:c[k] for k in ('project','snapshot','workspace_revision')}
        with patch('athena.ai._request_json',return_value={'message':{'content':'Ошибочный ответ'}}):
            started=self.request('agent/start',{'goal':'Как именовать методы?','mode':'analyze','context':context},'developer')
            end=time.monotonic()+3
            while self.w.code_agent.active and time.monotonic()<end:time.sleep(.01)
        rows=self.request('review/requests',user='leader')['items'];self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['login'],'developer')
        detail=self.request('review/request?id='+rows[0]['id'],user='leader')
        self.assertIn('Ошибочный ответ',detail['response']);self.assertEqual(detail['project'],c['project'])
        with patch.object(self.w.document_knowledge,'schedule') as schedule:
            feedback=self.request('review/correct',{'id':rows[0]['id'],'content':'Все новые методы имеют префикс АТН_.'},'leader')
            schedule.assert_called_once()
        self.assertIn('АТН_',self.w.document_knowledge.read(c['project'],feedback['document_id'])['content'])
        with self.assertRaises(HTTPError):self.request('agent/job?id='+started['id'],user='otherdev')
        with self.assertRaises(HTTPError):self.request('agent/cancel',{'id':started['id']},'otherdev')
        self.assertEqual(self.request('agent/status',user='otherdev')['jobs'],[])

    def test_revocation_ownership_and_reviewable_reverse_change_set(self):
        value=self.request('change-sets',{'action':'create','title':'Правка разработчика'},'developer')
        info=self.w.catalog.file(self.fixture.file_id);original=info['content'];changed='// Пояснение\n'+original
        value=self.request('change-sets',{'action':'add','id':value['id'],'revision':value['revision'],'entry':{'kind':'module','file_id':self.fixture.file_id,'base_hash':info['hash'],'content':changed}},'developer')
        with self.assertRaises(HTTPError):self.request('change-sets',{'action':'archive','id':value['id'],'revision':value['revision']},'otherdev')
        with self.assertRaises(HTTPError):self.request('review/undo',{'id':value['id'],'revision':value['revision']},'developer')
        self.request('review/undo',{'id':value['id'],'revision':value['revision']},'leader')
        revoked=self.w.change_sets.read(value['id']);self.assertTrue(revoked['revoked'])
        with self.assertRaises(HTTPError):self.request('change-sets',{'action':'archive','restore':True,'id':value['id'],'revision':revoked['revision']},'developer')
        value.update(status='applied',revoked=False);self.w.change_sets.write(value)
        self.fixture.file_path.write_text(changed,encoding='utf-8')
        result=self.request('review/undo',{'id':value['id'],'revision':value['revision']},'leader')
        reverse=self.w.change_sets.read(result['set_id'])
        self.assertEqual(reverse['entries'][0]['content'],original.replace('\r\n','\n'))
        self.assertEqual(reverse['entries'][0]['base_hash'],text_hash(changed))
        self.assertEqual(read_text(self.fixture.file_path).replace('\r\n','\n'),changed)
