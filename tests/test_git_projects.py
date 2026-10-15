import shutil
import subprocess
import tempfile
import unittest
import copy
import time
from unittest.mock import patch
from pathlib import Path

from athena.git_projects import GitProjects, remote_url
from athena.ai import context_pack


@unittest.skipUnless(shutil.which('git'), 'Git is required')
class GitProjectTests(unittest.TestCase):
    def git(self, path, *args):
        result=subprocess.run(['git','-C',str(path),*args],capture_output=True,encoding='utf-8')
        self.assertEqual(result.returncode,0,result.stderr)
        return result.stdout.strip()

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.remote=self.root/'remote.git';self.remote.mkdir();self.git(self.remote,'init','--bare','--initial-branch=main')
        self.author=self.root/'author';self.author.mkdir();self.git(self.author,'init','--initial-branch=main')
        self.identity(self.author)
        (self.author/'guide.md').write_text('Расчёт скидки: десять процентов.\n',encoding='utf-8')
        self.git(self.author,'add','.');self.git(self.author,'commit','-m','initial')
        self.git(self.author,'remote','add','origin',str(self.remote));self.git(self.author,'push','-u','origin','main')
        self.sources=GitProjects(self.root/'state')
        self.repo=self.sources.connect('project',{'url':str(self.remote),'name':'Документация'})
        self.clone=Path(self.repo['path']);self.identity(self.clone)

    def identity(self,path):
        self.git(path,'config','user.name','Test');self.git(path,'config','user.email','test@example.invalid')

    def tearDown(self):
        self.sources.close()
        # Windows Git marks object files read-only; TemporaryDirectory handles them.
        self.temp.cleanup()

    def action(self,action,**values):
        return self.sources.action('project',{'id':self.repo['id'],'action':action,**values})

    def test_multiple_sources_scoping_disabling_and_persistence(self):
        self.sources.connect('another',{'url':str(self.remote)})
        self.assertEqual(len(self.sources.listing('project')['repositories']),1)
        found=self.sources.search('project','скидки')['items'][0]
        read=self.sources.read_snapshot('project',found['id'])
        self.assertIn('десять',read['content']);self.assertTrue(read['enabled'])
        with self.assertRaises(ValueError):self.sources.read_snapshot('unrelated',found['id'])
        self.assertEqual(found['source'],'git');self.assertIn('десять',found['content']);self.assertEqual(found['start_line'],1)
        self.assertEqual(self.sources.search('unrelated','скидки')['items'],[])
        with self.assertRaises(ValueError):self.sources.repo('another',self.repo['id'])
        before=self.sources.revision('project')
        self.action('configure',enabled=False,automatic=False)
        self.assertEqual(self.sources.search('project','скидки')['items'],[])
        self.assertNotEqual(before,self.sources.revision('project'))
        self.sources.close();self.sources=GitProjects(self.root/'state')
        self.assertFalse(self.sources.listing('project')['repositories'][0]['enabled'])

    def test_fetch_ff_dirty_divergence_and_deleted_sources(self):
        (self.author/'guide.md').write_text('Новая скидка: двадцать процентов.',encoding='utf-8')
        self.git(self.author,'commit','-am','update');self.git(self.author,'push')
        self.action('sync');self.assertIn('двадцать',self.sources.search('project','скидка')['items'][0]['content'])
        (self.clone/'local.md').write_text('Локальная правка',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'локальные'):self.action('sync')
        self.assertTrue((self.clone/'local.md').exists())
        self.git(self.clone,'add','.');self.git(self.clone,'commit','-m','local')
        self.git(self.author,'rm','guide.md');self.git(self.author,'commit','-m','delete');self.git(self.author,'push')
        with self.assertRaises(ValueError):self.action('sync')
        self.assertTrue((self.clone/'guide.md').exists())
        (self.clone/'guide.md').unlink();self.action('refresh')
        self.assertEqual(self.sources.search('project','двадцать')['items'],[])

    def test_edit_review_stale_commit_selected_paths_and_push(self):
        source=self.action('read',path='guide.md')
        self.action('write',path='guide.md',content='Скидка: пятнадцать процентов.',expected_hash=source['hash'])
        with self.assertRaisesRegex(ValueError,'изменился'):
            self.action('write',path='guide.md',content='overwrite',expected_hash=source['hash'])
        review=self.action('review',paths=['guide.md'])
        self.assertIn('пятнадцать',review['diff'])
        (self.clone/'guide.md').write_text('Скидка: тридцать процентов.',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'просмотра'):self.action('commit',paths=review['paths'],token=review['token'],message='stale')
        (self.clone/'other.md').write_text('Do not commit',encoding='utf-8')
        review=self.action('review',paths=['guide.md'])
        result=self.action('commit',paths=review['paths'],token=review['token'],message='Update discount')
        self.assertEqual(self.git(self.clone,'show','--pretty=','--name-only','HEAD'),'guide.md')
        with self.assertRaises(ValueError):self.action('push',head='old')
        self.action('push',head=result['head'],branch=result['branch'])
        self.assertEqual(self.git(self.remote,'rev-parse','main'),result['head'])
        self.assertTrue((self.clone/'other.md').exists())

    def test_traversal_secrets_and_staged_changes_rejected(self):
        for name in ['../outside.md','.git/config','.env.local','secrets.json','/tmp/a.md','a\\b.md']:
            with self.assertRaises(ValueError):self.action('write',path=name,content='x',expected_hash=None)
        (self.clone/'guide.md').write_text('Скидка изменилась',encoding='utf-8')
        self.git(self.clone,'add','guide.md')
        review=self.action('review',paths=['guide.md'])
        with self.assertRaisesRegex(ValueError,'индексе'):self.action('commit',paths=review['paths'],token=review['token'],message='No')
        for value in ['--upload-pack=bad','https://token@github.com/a/b','https://x:y@github.com/a/b','ext::bad','https://host/a?token=secret']:
            with self.assertRaises(ValueError):remote_url(value)

    def test_empty_repository_initial_commit_and_branch(self):
        empty=self.root/'empty.git';empty.mkdir();self.git(empty,'init','--bare','--initial-branch=main')
        repo=self.sources.connect('empty',{'url':str(empty)})
        self.identity(repo['path']);data={'id':repo['id']}
        self.sources.action('empty',dict(data,action='write',path='README.md',content='Первый документ',expected_hash=None))
        review=self.sources.action('empty',dict(data,action='review',paths=['README.md']))
        result=self.sources.action('empty',dict(data,action='commit',paths=review['paths'],token=review['token'],message='Initial'))
        self.sources.action('empty',dict(data,action='push',head=result['head'],branch=result['branch']))
        branch=self.sources.action('empty',dict(data,action='branch',branch='athena/docs'))
        self.assertEqual(branch['branch'],'athena/docs')

    def test_rag_pack_can_answer_from_git_without_primary_code_hits(self):
        sources=context_pack(None,{'results':[],'snapshot':'test','references':self.sources.search('project','скидки')['items']},8000)
        self.assertEqual(sources[0]['type'],'reference');self.assertIn('десять',sources[0]['context'])
        self.assertTrue(sources[0]['file_hash'])

    def test_watch_detects_local_edits_and_remote_changes_and_retains_dirty_files(self):
        self.action('configure',enabled=True,automatic=True)
        (self.author/'guide.md').write_text('Скидка обновлена автоматически',encoding='utf-8')
        self.git(self.author,'commit','-am','Auto');self.git(self.author,'push')
        self.sources.check_once()
        self.assertIn('автоматически',self.sources.search('project','скидка')['items'][0]['content'])
        (self.clone/'guide.md').write_text('Локальное изменение скидки',encoding='utf-8')
        self.sources.check_once()
        self.assertIn('Локальное',self.sources.search('project','скидки')['items'][0]['content'])
        self.assertIn('локальные',self.sources.listing('project')['repositories'][0]['error'])

    def test_import_current_configuration_and_disconnect_preserve_clone(self):
        from athena.catalog import Catalog
        catalog=Catalog(Path(__file__).resolve().parents[1]/'examples'/'demo');catalog.scan()
        self.sources.import_project('project',{'id':self.repo['id'],'prefix':'configuration'},catalog)
        self.assertTrue((self.clone/'configuration'/'Configuration.xml').is_file())
        self.assertTrue(list((self.clone/'configuration').rglob('*.bsl')))
        self.action('disconnect')
        self.assertTrue(self.clone.is_dir())
        self.assertEqual(self.sources.search('project','скидки')['items'],[])

    def test_git_sources_reach_agent_tools_prompt_and_revision_guard(self):
        from test_agent_tools import AgentTests
        from test_code_agent import reply
        from athena.ai import DEFAULTS
        fixture=AgentTests();fixture.setUp()
        try:
            w=fixture.w;c=w.agent_tools.context()
            repo=w.git_projects.connect(c['project'],{'url':str(self.remote),'name':'Git Docs'})
            self.assertTrue(fixture.call('search',{'query':'скидки','scope':'git'})['items'])
            self.assertTrue(w.ai_context('скидки')['references'])
            w.ai.configure({'provider':'ollama',**DEFAULTS['ollama'],'model':'test-tools'})
            captured=[]
            def answer(*args):captured.append(copy.deepcopy(args[3]));return reply(text='Десять процентов по Git Docs/guide.md.')
            context={k:c[k] for k in ('project','snapshot','workspace_revision')}
            with patch('athena.code_agent.model_turn',side_effect=answer):
                job=w.code_agent.start({'goal':'Какие скидки описаны в документации?','mode':'analyze','context':context})
                deadline=time.monotonic()+5
                while w.code_agent.active and time.monotonic()<deadline:time.sleep(.01)
            self.assertEqual(w.code_agent.job(job['id'])['status'],'ready')
            self.assertIn('десять',str(captured))
            def change(*args):
                w.git_projects.action(c['project'],{'id':repo['id'],'action':'configure','enabled':False,'automatic':False})
                return reply(text='stale')
            with patch('athena.code_agent.model_turn',side_effect=change):
                job=w.code_agent.start({'goal':'Проверь скидки','mode':'analyze','context':context})
                deadline=time.monotonic()+5
                while w.code_agent.active and time.monotonic()<deadline:time.sleep(.01)
            self.assertEqual(w.code_agent.job(job['id'])['status'],'stopped')
        finally:fixture.tearDown()


if __name__=='__main__': unittest.main()
