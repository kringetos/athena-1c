import json
import threading
import time
import unittest
from unittest.mock import patch
import test_agent_tools as fixtures
from athena.ai import DEFAULTS
from athena.documentation import Documentation, instructions

class DocumentationTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.AgentTests();self.fixture.setUp();self.w=self.fixture.w;self.docs=self.w.documentation
        self.w.ai.configure({'provider':'ollama',**DEFAULTS['ollama'],'model':'documentation-test'})

    def tearDown(self):
        self.w.close();self.fixture.tearDown()

    def request(self,scope='project',**extra):
        c=self.w.agent_tools.context()
        return {'title':'Архитектура проекта','kind':'Архитектура','scope':scope,
                'context':{k:c[k] for k in ['project','snapshot','workspace_revision']},**extra}

    def finished(self,started):
        end=time.monotonic()+3
        while self.w.ai.active and time.monotonic()<end:time.sleep(.01)
        self.assertIsNone(self.w.ai.active)
        return self.docs.read(started['id'])

    def test_project_uses_skill_template_and_actual_sources_persists_and_edits(self):
        captured=[]
        def model(*args,**kwargs):
            captured.append(args[3]);return '# Архитектура\n\nСтатический обзор.',False
        before=self.fixture.file_path.read_bytes()
        with patch('athena.documentation.generate',side_effect=model):job=self.finished(self.docs.start(self.request()))
        self.assertEqual(job['status'],'ready',job)
        self.assertIn('Documentation Generator',captured[0][0]['content'])
        self.assertIn('DOCUMENT_TEMPLATE.md',captured[0][0]['content'])
        material=json.loads(captured[0][1]['content'])
        self.assertTrue(material['sources']);self.assertIn('Процедура',str(material['sources']))
        self.assertTrue(job['warnings']);self.assertNotIn('content',job['sources'][0])
        self.assertEqual(self.fixture.file_path.read_bytes(),before)
        restored=Documentation(self.w,self.fixture.root)
        self.assertEqual(restored.read(job['id'])['content'],job['content'])
        restored.save({'id':job['id'],'content':'# Исправленная документация'})
        self.assertEqual(restored.read(job['id'])['content'],'# Исправленная документация')
        self.assertEqual(len(restored.listing()['documents']),1)

    def test_selection_uses_utf16_offsets_and_unsaved_draft(self):
        identity=self.fixture.file_id;info=self.w.catalog.file(identity)
        content='// 😀 начало\nПроцедура Выбранная()\nКонецПроцедуры\n'
        selected='Процедура Выбранная()';a=content.index(selected)
        data=self.request('selection',file_id=identity,hash=info['hash'],draft=content,
             start=len(content[:a].encode('utf-16-le'))//2,end=len(content[:a+len(selected)].encode('utf-16-le'))//2)
        material=self.docs.prepare(data,10000)
        source=material['sources'][0]
        self.assertEqual(source['content'],selected);self.assertEqual(source['start_line'],2);self.assertTrue(source['draft'])
        self.assertNotIn('😀',source['content'])
        with patch('athena.documentation.generate',return_value=('# Описание\nКод',False)):
            job=self.finished(self.docs.start(data))
        self.assertEqual(job['status'],'ready',job)
        self.assertTrue(any('черновик' in w for w in job['warnings']))

    def test_rejects_stale_hash_context_empty_and_large_selection(self):
        info=self.w.catalog.file(self.fixture.file_id)
        data=self.request('selection',file_id=self.fixture.file_id,hash=info['hash'],start=0,end=0)
        with self.assertRaises(ValueError):self.docs.prepare(data,10000)
        data['end']=1;data['hash']='stale'
        with self.assertRaisesRegex(ValueError,'изменился'):self.docs.prepare(data,10000)
        data['hash']=info['hash'];data['context']['workspace_revision']+=1
        with self.assertRaisesRegex(ValueError,'Проект обновился'):self.docs.prepare(data,10000)
        with self.assertRaises(ValueError):self.docs.read('../secrets')
        data=self.request('module',file_id=self.fixture.file_id,hash=info['hash'],draft='x'*12000)
        with self.assertRaisesRegex(ValueError,'не помещается'):self.docs.prepare(data,2000)

    def test_cancel_retains_transport_lock_and_discards_late_answer(self):
        entered=threading.Event();release=threading.Event()
        def model(*args,**kwargs):entered.set();release.wait(2);return '# Поздний ответ',False
        with patch('athena.documentation.generate',side_effect=model):
            started=self.docs.start(self.request());self.assertTrue(entered.wait(1))
            self.docs.cancel(started['id']);self.assertIsNotNone(self.w.ai.active)
            with self.assertRaises(ValueError):self.docs.start(self.request())
            release.set();job=self.finished(started)
        self.assertEqual(job['status'],'cancelled');self.assertEqual(job['content'],'')

    def test_source_modified_during_generation_is_not_published_as_current(self):
        def model(*args,**kwargs):
            self.fixture.file_path.write_text('// изменено',encoding='utf-8');return '# Устаревший документ',False
        with patch('athena.documentation.generate',side_effect=model):job=self.finished(self.docs.start(self.request()))
        self.assertEqual(job['status'],'error',job);self.assertEqual(job['content'],'')

    def test_partial_context_and_truncated_output_are_explicit(self):
        data=self.request()
        with patch('athena.documentation.generate',return_value=('<!-- инструкция -->\n# Тест\n{{Название}}',True)):
            job=self.finished(self.docs.start(data))
        self.assertEqual(job['status'],'ready',job)
        self.assertNotIn('<!--',job['content']);self.assertNotIn('{{',job['content'])
        self.assertTrue(any('неполный' in w for w in job['warnings']))

    def test_line_endings_do_not_turn_saved_source_into_a_draft(self):
        info=self.w.catalog.file(self.fixture.file_id)
        from athena.catalog import canonical
        material=self.docs.prepare(self.request('module',file_id=self.fixture.file_id,hash=info['hash'],draft=canonical(info['content'])),10000)
        self.assertFalse(material['sources'][0]['draft'])

    def test_external_transmission_permission_is_enforced(self):
        self.w.ai.configure({'provider':'openai',**DEFAULTS['openai'],'base_url':'https://example.org/v1','model':'test','allow_remote_code':False})
        with self.assertRaisesRegex(ValueError,'Разрешите'):self.docs.start(self.request())

    def test_pdf_exports_editor_text_without_changing_document_or_sources(self):
        before=self.fixture.file_path.read_bytes()
        with patch('athena.documentation.generate',return_value=('# Документ\nСохранено.',False)):
            job=self.finished(self.docs.start(self.request()))
        edited='# Текущая редакция\n\n**Кириллица** и `Код()`.\n\n```bsl\nСообщить("Тест");\n```'
        from athena.document_pdf import render_pdf
        with patch('athena.document_pdf.render_pdf',wraps=render_pdf) as render:
            pdf,title=self.docs.pdf({'id':job['id'],'content':edited})
        self.assertTrue(pdf.startswith(b'%PDF-'))
        self.assertIn(b'/ToUnicode',pdf)
        self.assertEqual(title,job['title'])
        self.assertEqual(render.call_args.args[1],edited)
        self.assertEqual(render.call_args.args[0]['sources'],job['sources'])
        self.assertEqual(self.docs.read(job['id'])['content'],job['content'])
        self.assertEqual(self.fixture.file_path.read_bytes(),before)
        for invalid in ['',None,42,'x'*100001]:
            with self.assertRaises(ValueError):self.docs.pdf({'id':job['id'],'content':invalid})
        with patch.object(self.docs,'context',return_value={'project':'another-project'}):
            with self.assertRaises(ValueError):self.docs.pdf({'id':job['id']})
        job['status']='cancelled';self.docs.persist(job);self.docs.jobs.pop(job['id'],None)
        with self.assertRaises(ValueError):self.docs.pdf({'id':job['id']})

    def test_pdf_handles_multipage_tables_long_code_and_literal_html(self):
        from athena.document_pdf import render_pdf
        text='# Обзор\n\n| Объект | Назначение |\n|---|---|\n'
        text+=''.join('| Справочник | '+('Очень длинное описание ' * 100 if i==20 else 'Описание')+' |\n' for i in range(100))
        text+='\n```bsl\n'+('ДлинныйИдентификатор'*200)+'\n'+('Сообщить("Привет");\n'*150)+'```\n\n'
        text+='<script>alert("test")</script>\n\n![Картинка](http://127.0.0.1:9/private)'
        with patch('urllib.request.urlopen',side_effect=AssertionError('No network for PDF')):
            pdf=render_pdf({'title':'Проверка','warnings':['Неполный контекст'],'sources':[]},text)
        self.assertTrue(pdf.startswith(b'%PDF-'))
        self.assertGreater(pdf.count(b'/Type /Page'),3)

if __name__=='__main__':unittest.main()
