"""Versioned local platform reference, independent of project/vector storage."""
from contextlib import contextmanager
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
import hashlib
import json
import re
import sqlite3
import struct
import zipfile

PARSER_VERSION = 'hbk-pages-v1'
BOOKS = ('shcntx_ru.hbk', 'shlang_ru.hbk', 'shquery_ru.hbk')


def container_block(data, position):
    chunks = []; seen = set(); total = None; received = 0
    while position != 0x7fffffff:
        if position in seen or position < 16 or position + 31 > len(data):
            raise ValueError('Повреждённая цепочка блоков HBK.')
        seen.add(position)
        header = data[position:position + 31]
        if not re.fullmatch(rb'\r\n[0-9a-fA-F]{8} [0-9a-fA-F]{8} [0-9a-fA-F]{8} \r\n', header):
            raise ValueError('Неизвестный формат блока HBK.')
        length, capacity, following = [int(value, 16) for value in header.split()]
        if total is None: total = length
        size = min(capacity, total - received)
        if size < 0 or position + 31 + size > len(data):
            raise ValueError('Обрезанный блок HBK.')
        chunks.append(data[position + 31:position + 31 + size]); received += size
        position = following
    if received != total: raise ValueError('Неполный блок HBK.')
    return b''.join(chunks)


def file_storage(data):
    if len(data) < 16: raise ValueError('Пустой контейнер HBK.')
    toc = container_block(data, 16)
    if len(toc) % 12: raise ValueError('Повреждённое оглавление HBK.')
    for header, content, _ in struct.iter_unpack('<III', toc):
        name = container_block(data, header)[20:].decode('utf-16le').rstrip('\0')
        if name == 'FileStorage': return container_block(data, content)
    raise ValueError('В HBK отсутствует FileStorage.')


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []; self.title_parts = []; self.heading = False; self.ignore = 0

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style', 'head'}: self.ignore += 1
        if self.ignore: return
        if tag == 'h1': self.heading = True
        if tag in {'p', 'div', 'br', 'hr', 'tr', 'li', 'pre', 'h1', 'h2', 'h3'}: self.parts.append('\n')
        if tag in {'td', 'th'}: self.parts.append(' | ')

    def handle_endtag(self, tag):
        if tag in {'script', 'style', 'head'}:
            self.ignore = max(0, self.ignore - 1); return
        if self.ignore: return
        if tag == 'h1': self.heading = False
        if tag in {'p', 'div', 'tr', 'li', 'pre', 'h1', 'h2', 'h3'}: self.parts.append('\n')

    def handle_data(self, data):
        if self.ignore: return
        self.parts.append(data)
        if self.heading: self.title_parts.append(data)

    def result(self, path):
        lines = [re.sub(r'[\t \xa0]+', ' ', line).strip() for line in ''.join(self.parts).splitlines()]
        text = '\n'.join(line for line in lines if line)
        title = ''.join(self.title_parts).strip() or Path(path).stem
        labels = ('Синтаксис:', 'Параметры:', 'Возвращаемое значение:', 'Описание:', 'Доступность:', 'Пример:', 'Примечание:')
        sections = {}; current = None
        for line in text.splitlines():
            label = next((label for label in labels if line.startswith(label)), None)
            if label: current = label[:-1]; sections.setdefault(current, []).append(line[len(label):].strip())
            elif current: sections[current].append(line)
        return title, text, {key: '\n'.join(value).strip() for key, value in sections.items()}


class Reference:
    def __init__(self, path):
        self.path = Path(path); self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY, version TEXT, root TEXT, fingerprint TEXT, pages INTEGER);
CREATE TABLE IF NOT EXISTS pages(id TEXT PRIMARY KEY, source TEXT REFERENCES sources(id), book TEXT, path TEXT, title TEXT, body TEXT, sections TEXT);
CREATE INDEX IF NOT EXISTS pages_source ON pages(source);
CREATE VIRTUAL TABLE IF NOT EXISTS page_search USING fts5(id UNINDEXED,source UNINDEXED,title,body,tokenize='unicode61');
''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30); db.row_factory = sqlite3.Row
        db.create_function('casefold', 1, lambda value: value.casefold())
        db.execute('PRAGMA foreign_keys=ON')
        try: yield db
        finally: db.close()

    def sources(self):
        with self.connect() as db: return [dict(row) for row in db.execute('SELECT * FROM sources ORDER BY version')]

    def import_platform(self, platform, progress=lambda *args: None):
        executable = Path(platform).expanduser().resolve()
        if not executable.is_file() or executable.name.lower() != '1cv8.exe':
            raise ValueError('Укажите существующий 1cv8.exe выбранной платформы.')
        root = executable.parent
        books = [(root / name) for name in BOOKS if (root / name).is_file()]
        if not books or books[0].name != BOOKS[0]: raise ValueError('Рядом с 1cv8.exe отсутствует shcntx_ru.hbk.')
        source = hashlib.sha256(str(root).casefold().encode()).hexdigest()
        version = root.parent.name if root.name.lower() == 'bin' else root.name
        fingerprint = hashlib.sha256(PARSER_VERSION.encode())
        for book in books:
            with book.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''): fingerprint.update(chunk)
        fingerprint = fingerprint.hexdigest()
        with self.connect() as db:
            existing = db.execute('SELECT * FROM sources WHERE id=?', (source,)).fetchone()
            if existing and existing['fingerprint'] == fingerprint: return dict(existing)
            # A failed import rolls back completely; readers retain the previous reference.
            try:
                db.execute('BEGIN IMMEDIATE')
                db.execute('DELETE FROM page_search WHERE source=?', (source,))
                db.execute('DELETE FROM pages WHERE source=?', (source,))
                db.execute('INSERT OR REPLACE INTO sources VALUES(?,?,?,?,0)', (source, version, str(root), fingerprint))
                count = 0
                for book in books:
                    with zipfile.ZipFile(BytesIO(file_storage(book.read_bytes()))) as archive:
                        entries = [info for info in archive.infolist() if info.filename.lower().endswith(('.html', '.htm'))]
                        for index, info in enumerate(entries):
                            if info.file_size > 8 * 1024 * 1024: raise ValueError('Слишком большая страница справки.')
                            parser = PageParser(); parser.feed(archive.read(info).decode('utf-8-sig'))
                            title, body, sections = parser.result(info.filename)
                            if not body: continue
                            page = hashlib.sha256((source + book.name + info.filename).encode()).hexdigest()
                            db.execute('INSERT INTO pages VALUES(?,?,?,?,?,?,?)', (page, source, book.name, info.filename, title, body, json.dumps(sections, ensure_ascii=False)))
                            db.execute('INSERT INTO page_search VALUES(?,?,?,?)', (page, source, title, body))
                            count += 1
                            if index % 250 == 0: progress(book.name, index, len(entries))
                db.execute('UPDATE sources SET pages=? WHERE id=?', (count, source)); db.commit()
            except Exception:
                db.rollback(); raise
        return {'id': source, 'version': version, 'root': str(root), 'fingerprint': fingerprint, 'pages': count}

    def search(self, query, source, limit=30):
        if not isinstance(query, str) or len(query) > 500: raise ValueError('Запрос должен быть строкой до 500 символов.')
        if not source: raise ValueError('Выберите версию платформы.')
        tokens = re.findall(r'\w+', query, re.UNICODE)
        if not tokens: return []
        match = ' AND '.join('"' + word + '"*' for word in tokens)
        with self.connect() as db:
            return [dict(row) for row in db.execute('''
SELECT p.id,p.title,p.book,p.path,s.version,snippet(page_search,3,'','', ' … ',24) AS excerpt
FROM page_search JOIN pages p ON p.id=page_search.id JOIN sources s ON s.id=p.source
WHERE page_search MATCH ? AND p.source=?
ORDER BY CASE WHEN casefold(CASE WHEN instr(p.title,' (')>0 THEN substr(p.title,1,instr(p.title,' (')-1) ELSE p.title END)=? THEN 0 ELSE 1 END,bm25(page_search,0,0,10,1) LIMIT ?
''', (match, source, query.casefold(), min(max(int(limit), 1), 100)))]

    def page(self, identifier):
        with self.connect() as db:
            row = db.execute('SELECT p.*,s.version FROM pages p JOIN sources s ON s.id=p.source WHERE p.id=?', (identifier,)).fetchone()
        if not row: raise ValueError('Страница справки не найдена.')
        result = dict(row); result['sections'] = json.loads(result['sections']); return result
