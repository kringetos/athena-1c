from io import BytesIO
from pathlib import Path
import json
import struct
import tempfile
import threading
import unittest
import zipfile
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from athena.reference import Reference, container_block, file_storage
from athena.reference_server import ReferenceService, handler


def hbk(pages):
    stream = BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, html in pages.items(): archive.writestr(name, html.encode('utf-8'))
    payload = stream.getvalue()
    name = b'\0' * 20 + 'FileStorage\0'.encode('utf-16le')
    def block(value): return f'\r\n{len(value):08x} {len(value):08x} 7fffffff \r\n'.encode() + value
    header_position = 16 + 31 + 12
    content_position = header_position + 31 + len(name)
    return b'\0' * 16 + block(struct.pack('<III', header_position, content_position, 0x7fffffff)) + block(name) + block(payload)


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.reference = Reference(self.root / 'reference.sqlite3')
        self.platform = self.root / '8.5.1' / 'bin' / '1cv8.exe'
        self.platform.parent.mkdir(parents=True); self.platform.write_bytes(b'not executed')
        self.book = self.platform.parent / 'shcntx_ru.hbk'
        self.book.write_bytes(hbk({'objects/Table.html': '<h1>ТаблицаЗначений</h1><h2>Синтаксис:</h2><p>Новый ТаблицаЗначений</p><h2>Описание:</h2><p>Хранение данных 😀</p>', 'objects/Table/Find.html': '<h1>ТаблицаЗначений.Найти (ValueTable.Find)</h1><h2>Параметры:</h2><p>Значение &lt;Любой тип&gt;</p><script>evil</script>'}))

    def tearDown(self): self.temp.cleanup()

    def test_import_search_sections_and_cache(self):
        progress = []
        source = self.reference.import_platform(self.platform, lambda *args: progress.append(args))
        self.assertEqual(source['pages'], 2); self.assertEqual(source['version'], '8.5.1')
        row = self.reference.search('ТаблицаЗначений', source['id'])[0]
        self.assertEqual(row['title'], 'ТаблицаЗначений')
        page = self.reference.page(row['id'])
        self.assertEqual(page['sections']['Синтаксис'], 'Новый ТаблицаЗначений')
        self.assertIn('😀', page['body'])
        page = self.reference.page(self.reference.search('Find', source['id'])[0]['id'])
        self.assertNotIn('evil', page['body']); self.assertIn('<Любой тип>', page['body'])
        self.assertTrue(progress)
        progress.clear(); self.reference.import_platform(self.platform, lambda *args: progress.append(args))
        self.assertFalse(progress)
        self.assertEqual(self.reference.search('" OR *', source['id']), [])

    def test_versions_are_isolated_and_failed_import_keeps_previous(self):
        first = self.reference.import_platform(self.platform)
        second_platform = self.root / '8.3.27' / 'bin' / '1cv8.exe'
        second_platform.parent.mkdir(parents=True); second_platform.write_bytes(b'')
        (second_platform.parent / 'shcntx_ru.hbk').write_bytes(hbk({'new.html': '<h1>ДругойМетод</h1>'}))
        second = self.reference.import_platform(second_platform)
        self.assertEqual(self.reference.search('Таблица', second['id']), [])
        self.book.write_bytes(b'broken')
        with self.assertRaises(ValueError): self.reference.import_platform(self.platform)
        self.assertTrue(self.reference.search('Таблица', first['id']))
        self.assertEqual(len(self.reference.sources()), 2)

    def test_container_chain_and_cycle_rejected(self):
        data = b'\0' * 16 + b'\r\n00000006 00000003 00000032 \r\nabc' + b'\r\n00000003 00000003 7fffffff \r\ndef'
        self.assertEqual(container_block(data, 16), b'abcdef')
        with self.assertRaises(ValueError): container_block(data.replace(b'00000032', b'00000010'), 16)
        with self.assertRaises(ValueError): file_storage(data[:20])

    def test_exact_method_with_english_alias_ranks_before_prefix_match(self):
        self.book.write_bytes(hbk({'long.html': '<h1>Запрос.ВыполнитьПакет (Query.ExecuteBatch)</h1>', 'exact.html': '<h1>Запрос.Выполнить (Query.Execute)</h1>'}))
        source = self.reference.import_platform(self.platform)
        self.assertEqual(self.reference.search('запрос.выполнить', source['id'])[0]['path'], 'exact.html')

    def test_http_authentication_and_reference_access(self):
        service = ReferenceService(self.reference.path)
        source = service.reference.import_platform(self.platform)
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler(service))
        thread = threading.Thread(target=server.serve_forever); thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with self.assertRaises(HTTPError): urlopen(base + '/api/status')
            with urlopen(Request(base + '/api/search?q=Find&source=' + source['id'], headers={'X-Athena-Token': service.token})) as response:
                self.assertEqual(len(json.load(response)), 1)
            with self.assertRaises(HTTPError): urlopen(Request(base, headers={'Host': 'attacker.example'}))
        finally:
            server.shutdown(); server.server_close(); thread.join()
