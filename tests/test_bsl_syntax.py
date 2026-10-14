import unittest
from athena.bsl_index import extract, VERSION


class SyntaxTreeTests(unittest.TestCase):
    def test_fast_parse_and_ll_recovery_preserve_results(self):
        sources=[
            'Функция Сумма(Знач Число=0) Экспорт\nВозврат Общий.Метод(Число);\nКонецФункции',
            '&НаКлиенте\nАсинх Процедура Тест()\nОтвет=Ждать Получить();\nКонецПроцедуры',
            'Процедура Ошибка()\nЕсли Тогда\nКонецПроцедуры\nПроцедура Дальше()\nВызов();\nКонецПроцедуры',
            '#Если Сервер Тогда\nПроцедура Тест()\nЕсли Проверка() Тогда\nОбъект.Метод(Новый Структура("а",1));\nКонецЕсли;\nКонецПроцедуры\n#КонецЕсли',
        ]
        for source in sources:
            with self.subTest(source=source):self.assertEqual(extract(source),extract(source,fast=False))

    def test_unicode_cache_preserves_lexer_tokens(self):
        from antlr4 import InputStream,CommonTokenStream
        from antlr4.dfa.DFA import DFA
        from antlr4.PredictionContext import PredictionContextCache
        from athena.bsl_grammar.BSLLexer import BSLLexer
        from athena.bsl_index import UnicodeLexerSimulator
        source='// Комментарий 😀\n&НаСервере\nПроцедура Тест()\nОбъект.Метод("Привет 世界");\nКонецПроцедуры\n#Если Сервер Тогда\n#КонецЕсли'
        original=BSLLexer(InputStream(source));stream=CommonTokenStream(original);stream.fill()
        dfa=[DFA(state,i) for i,state in enumerate(BSLLexer.atn.decisionToState)]
        optimized=BSLLexer(InputStream(source));optimized._interp=UnicodeLexerSimulator(optimized,optimized.atn,dfa,PredictionContextCache())
        other=CommonTokenStream(optimized);other.fill()
        identity=lambda t:(t.type,t.channel,t.text,t.start,t.stop,t.line,t.column)
        self.assertEqual(list(map(identity,stream.tokens)),list(map(identity,other.tokens)))
        self.assertTrue(any(any(key>127 for key in state.edges) for decision in dfa for state in decision.states if state.edges))


    def test_async_parameters_directives_and_nested_calls(self):
        source = '''// Проверка загрузки
&НаКлиенте
Асинх Функция Загрузить(Знач Имя = "", Дата = '20261013') Экспорт
    Ответ = Ждать Получить(Проверить(Имя));
    Возврат Ответ;
КонецФункции'''
        result = extract(source)
        self.assertEqual(result['warnings'], [])
        method = result['symbols'][0]
        self.assertTrue(method['async'])
        self.assertTrue(method['syntax_valid'])
        self.assertEqual(method['declaration_line'], 3)
        self.assertEqual(method['parameter_details'][0],
                         {'name': 'Имя', 'by_value': True, 'default': '""', 'line': 3})
        self.assertEqual(method['parameter_details'][1]['default'], "'20261013'")
        self.assertEqual([c['name'] for c in method['calls']], ['Получить', 'Проверить'])
        self.assertIn('antlr', VERSION)

    def test_chained_receivers_and_constructor_are_distinct(self):
        result = extract('''Процедура Обработать()
    Структура = Новый Структура("Ключ", Получить());
    Объект.Коллекция[0].Выполнить(Вложенный());
    Фабрика().Сервис.Запустить();
КонецПроцедуры''')
        self.assertEqual(result['warnings'], [])
        calls = result['symbols'][0]['calls']
        self.assertEqual([c['name'] for c in calls],
                         ['Получить', 'Выполнить', 'Вложенный', 'Фабрика', 'Запустить'])
        self.assertEqual(calls[1]['receiver'], 'Объект.Коллекция[0]')
        self.assertEqual(calls[-1]['receiver'], 'Фабрика().Сервис')

    def test_strings_and_preprocessor_do_not_create_phantom_calls(self):
        result = extract('''#Область Методы
#Если Сервер Тогда
Функция Текст()
    Возврат "Функция Ложная()
    |Модуль.Ложная()
    |";
КонецФункции
#КонецЕсли
#КонецОбласти''')
        self.assertEqual(result['warnings'], [])
        self.assertEqual([s['name'] for s in result['symbols']], ['Текст'])
        self.assertEqual(result['symbols'][0]['calls'], [])

    def test_syntax_errors_have_positions_and_recover_next_method(self):
        result = extract('''Процедура СОшибка()
    Если Тогда
        Тест();
    КонецЕсли;
КонецПроцедуры
Функция Рабочая()
    Возврат 1;
КонецФункции''')
        self.assertTrue(result['warnings'])
        self.assertEqual(result['warnings'][0]['line'], 2)
        self.assertGreater(result['warnings'][0]['column'], 0)
        self.assertEqual([s['name'] for s in result['symbols']], ['СОшибка', 'Рабочая'])
        self.assertFalse(result['symbols'][0]['syntax_valid'])
        self.assertTrue(result['symbols'][1]['syntax_valid'])

    def test_unknown_characters_are_not_silently_ignored(self):
        result = extract('Процедура Тест()\n @\nКонецПроцедуры')
        self.assertTrue(result['warnings'])
        self.assertFalse(result['symbols'][0]['syntax_valid'])

    def test_local_names_include_assignments_loops_and_module_variables(self):
        result = extract('''Перем ОбщийМодуль;
Процедура Тест(Параметр)
    Перем Локальная;
    АвтоПеременная = 1;
    Для Каждого Строка Из Таблица Цикл
        Продолжить;
    КонецЦикла;
КонецПроцедуры''')
        self.assertEqual(result['warnings'], [])
        self.assertEqual(set(result['symbols'][0]['local_names']),
                         {'ОбщийМодуль', 'Параметр', 'Локальная', 'АвтоПеременная', 'Строка'})

    def test_english_crlf_and_case(self):
        result = extract('function Test(Val Arg = Undefined) Export\r\n Return helper();\r\nendfunction')
        self.assertEqual(result['warnings'], [])
        self.assertEqual(result['symbols'][0]['end_line'], 3)
        self.assertEqual(result['symbols'][0]['calls'][0]['name'], 'helper')

