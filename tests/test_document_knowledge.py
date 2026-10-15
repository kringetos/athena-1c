import base64
import copy
from io import BytesIO
import time
import unittest
from unittest.mock import patch
from zipfile import ZipFile
import numpy as np
import test_agent_tools as fixtures
from athena.document_knowledge import extract, DocumentKnowledge
from athena.ai import DEFAULTS
from test_code_agent import reply


class DocumentKnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.AgentTests();self.fixture.setUp();self.w=self.fixture.w
        self.kb=self.w.document_knowledge;self.project=self.w.agent_tools.context()['project']
        real=self.w.embedder
        class Embeddings:
            model_id=real.model_id;dimensions=real.dimensions
            def documents(self,texts):
                return [np.array([1]+[0]*(self.dimensions-1),dtype=np.float32) for _ in texts]
            def query(self,text):return self.documents([text])[0]
        self.embedding=Embeddings()
        self.patch=patch.object(self.kb,'runtime_for_settings',return_value=self.embedding);self.patch.start()

    def tearDown(self):
        self.kb.close();self.patch.stop();self.fixture.tearDown()

    def upload(self,text,name='Описание.md',rules=False):
        return self.kb.upload(self.project,{'name':name,'data':base64.b64encode(text.encode()).decode(),'rules':rules})['id']

    def finished(self):
        end=time.monotonic()+3
        while self.kb.worker and time.monotonic()<end:time.sleep(.01)
        self.assertIsNone(self.kb.worker)

    def test_upload_automatically_embeds_and_searches_with_sources(self):
        identity=self.upload('# Расчёты\nСкидка клиента равна десяти процентам.\n'*80)
        self.finished();state=self.kb.listing(self.project)['documents'][0]
        self.assertEqual(state['status'],'ready');self.assertGreater(state['done'],1)
        found=self.fixture.call('search',{'query':'скидка','scope':'documents'})
        self.assertTrue(found['items']);self.assertEqual(found['mode'],'hybrid')
        item=found['items'][0];self.assertEqual(item['id'],identity)
        self.assertIn('десяти процентам',item['content']);self.assertTrue(item['document_hash']);self.assertGreaterEqual(item['start_line'],1)
        self.assertEqual(self.kb.listing('other-project')['documents'],[])
        self.assertEqual(self.kb.search('other-project','скидка')['items'],[])
        with self.assertRaises(ValueError):self.kb.read('other-project',identity)
        self.assertEqual(self.kb.rules(self.project),[])

    def test_errors_retry_reuses_vectors_and_disabled_documents_are_excluded(self):
        with patch.object(self.embedding,'documents',side_effect=ValueError('CPU unavailable')):
            identity=self.upload('Скидка и стандарты обработки.');self.finished()
        self.assertEqual(self.kb.listing(self.project)['documents'][0]['status'],'error')
        self.kb.configure(self.project,{'id':identity,'enabled':True,'rules':False});self.finished()
        self.assertEqual(self.kb.listing(self.project)['documents'][0]['status'],'ready')
        self.kb.configure(self.project,{'id':identity,'enabled':False,'rules':False});self.finished()
        self.assertEqual(self.kb.search(self.project,'Скидка')['items'],[])
        with patch.object(self.embedding,'documents',side_effect=AssertionError('Must reuse cached vector')):
            self.kb.configure(self.project,{'id':identity,'enabled':True,'rules':True});self.finished()
        self.assertEqual(self.kb.rules(self.project)[0]['id'],identity)
        self.assertEqual(self.kb.listing(self.project)['documents'][0]['status'],'ready')

    def test_extraction_validation_duplicates_rule_budget_and_persistence(self):
        identity=self.upload('Всегда добавляй комментарий к экспортной функции.',rules=True);self.finished()
        with self.assertRaises(ValueError):self.upload('Всегда добавляй комментарий к экспортной функции.')
        with self.assertRaises(ValueError):self.upload('a'*12001,rules=True)
        self.assertEqual(extract('x.txt','Привет'.encode('cp1251')),'Привет')
        docx=BytesIO()
        with ZipFile(docx,'w') as z:z.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Привет DOCX</w:t></w:r></w:p></w:body></w:document>')
        self.assertIn('Привет DOCX',extract('test.docx',docx.getvalue()))
        from athena.document_pdf import render_pdf
        self.assertIn('Русский документ',extract('test.pdf',render_pdf({'title':'Тест'},'# Русский документ')))
        for name,data in [('x.exe',b'abc'),('x.pdf',b'bad'),('x.docx',b'bad'),('x.txt',b'')]:
            with self.assertRaises(ValueError):extract(name,data)
        with self.assertRaises(ValueError):self.kb.upload(self.project,{'name':'../x.md','data':'eA=='})
        with self.assertRaises(ValueError):self.kb.upload(self.project,{'name':'x.md','data':'***'})
        self.kb.close()
        restored=DocumentKnowledge(self.w,self.fixture.root)
        try:self.assertEqual(restored.rules(self.project)[0]['id'],identity)
        finally:restored.close()

    def test_lexical_fallback_while_index_is_unavailable(self):
        with patch.object(self.embedding,'documents',side_effect=ValueError('No model')):
            self.upload('Запрещено использовать глобальные переменные.');self.finished()
        found=self.kb.search(self.project,'глобальные')
        self.assertEqual(found['mode'],'lexical');self.assertTrue(found['items']);self.assertTrue(found['warning'])

    def test_rules_present_in_every_model_turn_and_change_stops_task(self):
        self.upload('Обязательный префикс имен: АТН_.','Правила.md',rules=True);self.finished()
        self.w.ai.configure({'provider':'ollama',**DEFAULTS['ollama'],'model':'test-tools'})
        c=self.w.agent_tools.context();context={k:c[k] for k in ('project','snapshot','workspace_revision')}
        captured=[];responses=[reply('get_configuration'),reply(text='Проверено с учётом правил.')]
        def model(*args):captured.append(copy.deepcopy(args[3]));return responses.pop(0)
        with patch('athena.code_agent.model_turn',side_effect=model):
            started=self.w.code_agent.start({'goal':'Проверь правила именования','mode':'analyze','context':context})
            end=time.monotonic()+3
            while self.w.code_agent.active and time.monotonic()<end:time.sleep(.01)
        job=self.w.code_agent.job(started['id']);self.assertEqual(job['status'],'ready',job)
        self.assertEqual(len(captured),2)
        for messages in captured:self.assertIn('Обязательный префикс имен: АТН_.',messages[0]['content'])
        self.assertEqual(job['document_rules'][0]['name'],'Правила.md')
        def change(*args):
            item=self.kb.rules(self.project)[0]
            self.kb.configure(self.project,{'id':item['id'],'enabled':False,'rules':True})
            return reply(text='Это уже устаревший ответ')
        with patch('athena.code_agent.model_turn',side_effect=change):
            started=self.w.code_agent.start({'goal':'Проверь правила','mode':'analyze','context':context})
            end=time.monotonic()+3
            while self.w.code_agent.active and time.monotonic()<end:time.sleep(.01)
        job=self.w.code_agent.job(started['id']);self.assertEqual(job['status'],'stopped');self.assertIn('правила',job['phase'])


if __name__=='__main__':unittest.main()
