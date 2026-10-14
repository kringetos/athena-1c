import unittest
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch
import test_language as fixture_module
from athena.completion import Completion, code_context, validate


class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.LanguageTests(); self.fixture.setUp()
        self.ai = SimpleNamespace(lock=threading.Lock(), active=None, status=lambda: {}, connection=Mock(return_value=('ollama', {'model': 'local', 'base_url': 'http://127.0.0.1:11434', 'timeout': 120, 'allow_remote_code': False}, '')))
        self.completion = Completion(self.fixture.root / 'completion.json', self.fixture.language, self.ai)
        self.completion.configure({'source': 'platform1'})

    def tearDown(self): self.fixture.tearDown()

    def suggest(self, before, **extra): return self.completion.suggest({'before': before, **extra}, self.fixture.snapshot)

    def test_type_inference_and_reassignment(self):
        result = self.suggest('Зап = Новый Запрос;\nЗап.Вып')
        self.assertEqual(result['replace'], 3)
        self.assertEqual(result['items'][0]['insert'], 'Выполнить')
        self.assertFalse(self.suggest('Зап = Новый Запрос;\nЗап = 5;\nЗап.Вып')['items'])

    def test_metadata_and_local_variables_parameters(self):
        self.assertIn('Номенклатура', [item['label'] for item in self.suggest('Справочники.Ном')['items']])
        self.assertIn('Аргумент', [item['label'] for item in self.suggest('Процедура Тест(Знач Аргумент)\n Арг')['items']])
        self.assertIn('Значение', [item['label'] for item in self.suggest('Значение = 1;\nЗна')['items']])

    def test_comments_strings_and_local_mode_never_call_ai(self):
        for text in ['// Запрос.Вып', 'Сообщить("Запрос.Вып', 'Сообщить("Строка ""цитата""']:
            self.assertFalse(self.suggest(text, neural=True)['items'])
        self.suggest('Запрос.Вып', neural=True)
        self.ai.connection.assert_not_called()
        self.assertEqual(code_context('"Текст"\nЕсли')[1], False)

    def test_active_agent_keeps_completion_local(self):
        self.completion.configure({'mode':'ai','source':'platform1'})
        self.ai.active='agent:running'
        with patch('athena.completion.generate') as model:
            result=self.suggest('Зап = Новый Запрос;\nЗап.Вып',neural=True)
        model.assert_not_called();self.assertTrue(result['items']);self.assertIn('занята',result['notice'])

    def test_settings_persistence_and_validation(self):
        self.completion.configure({'enabled': False, 'source': 'platform1', 'trigger': 'manual', 'shortcut': 'alt-slash'})
        restored = Completion(self.completion.path, self.fixture.language, self.ai)
        self.assertEqual(restored.config, self.completion.config)
        self.assertFalse(self.suggest('Запрос.Вып')['items'])
        for data in [{'mode': 'unknown'}, {'delay_ms': True}, {'min_chars': 0}, {'trigger': 'unknown'}]:
            with self.assertRaises(ValueError): validate(data)

    def test_ai_is_bounded_and_remote_consent_enforced(self):
        self.completion.configure({'mode': 'ai', 'source': 'platform1'})
        with patch('athena.completion.generate', return_value=('Возврат Истина;', False)) as generate:
            result = self.suggest('Запрос.Вып', neural=True)
            self.assertEqual(result['items'][0]['origin'], 'ai'); self.assertEqual(result['items'][0]['replace'], 0)
            self.assertEqual(generate.call_args.kwargs['max_tokens'], 256)
            self.assertEqual(generate.call_args.args[1]['timeout'], 30)
        self.ai.connection.return_value = ('openai', {'model': 'remote', 'base_url': 'https://example.org/v1', 'timeout': 120, 'allow_remote_code': False}, 'secret')
        with patch('athena.completion.generate') as generate:
            result = self.suggest('Запрос.Вып', neural=True)
            generate.assert_not_called(); self.assertTrue(result['items']); self.assertIn('разрешение', result['notice'])

    def test_one_ai_request_at_a_time_and_error_fallback(self):
        self.completion.configure({'mode': 'ai', 'source': 'platform1'})
        self.completion.ai_lock.acquire()
        try:
            self.assertTrue(self.suggest('Запрос.Вып', neural=True)['items'])
            self.ai.connection.assert_not_called()
        finally: self.completion.ai_lock.release()
        with patch('athena.completion.generate', side_effect=ValueError('Модель недоступна')):
            result = self.suggest('Запрос.Вып', neural=True)
            self.assertTrue(result['items']); self.assertEqual(result['notice'], 'Модель недоступна')
