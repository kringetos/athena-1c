"""Shared, version-scoped language knowledge for the editor and agent tools."""
import json
import re
import threading
from pathlib import Path
import sqlite3
from contextlib import contextmanager
from athena.knowledge import Knowledge


def platform_symbol(page):
    title = page['title']
    match = re.fullmatch(r'(.*?)\s+\((.*)\)', title)
    name, alias = match.groups() if match else (title, '')
    path = page['path'].lower()
    kind = next((kind for marker, kind in [('/methods/', 'method'), ('/properties/', 'property'), ('/events/', 'event'), ('/ctors/', 'constructor'), ('/constructors/', 'constructor')] if marker in path), 'article')
    if kind == 'article' and '/objects/' in '/' + path and '.' not in name and page['sections'].get('Описание'):
        kind = 'type'
    owner = name.rsplit('.', 1)[0] if kind in {'method', 'property', 'event', 'constructor'} and '.' in name else ''
    return {'id': page['id'], 'origin': 'platform', 'name': name, 'alias': alias,
            'short_name': name.rsplit('.', 1)[-1], 'owner': owner, 'kind': kind,
            'signature': page['sections'].get('Синтаксис', ''),
            'parameters_text': page['sections'].get('Параметры', ''),
            'returns_text': page['sections'].get('Возвращаемое значение', ''),
            'description': page['sections'].get('Описание', ''),
            'availability': page['sections'].get('Доступность', ''),
            'location': {'version': page['version'], 'source': page['source'], 'book': page['book'], 'path': page['path']}}


class LanguageKnowledge:
    def __init__(self, reference, project_database):
        self.reference = reference; self.project_database = Path(project_database)
        self.reader = Knowledge(self.project_database, read_only=True)
        self.lock = threading.RLock(); self.indexed_snapshots = set()
        with reference.connect() as db:
            db.executescript('''
CREATE TABLE IF NOT EXISTS language_versions(source TEXT PRIMARY KEY, fingerprint TEXT, recipe TEXT);
CREATE TABLE IF NOT EXISTS platform_symbols(id TEXT PRIMARY KEY,source TEXT,name TEXT,short_name TEXT,alias TEXT,short_alias TEXT,owner TEXT,record TEXT);
CREATE INDEX IF NOT EXISTS platform_names ON platform_symbols(source,name);
CREATE INDEX IF NOT EXISTS platform_short_names ON platform_symbols(source,short_name);
CREATE INDEX IF NOT EXISTS platform_aliases ON platform_symbols(source,alias);
CREATE INDEX IF NOT EXISTS platform_short_aliases ON platform_symbols(source,short_alias);
CREATE INDEX IF NOT EXISTS platform_owners ON platform_symbols(source,owner);
CREATE TABLE IF NOT EXISTS project_objects(snapshot TEXT,id TEXT,name TEXT,synonym TEXT,parent TEXT,record TEXT,PRIMARY KEY(snapshot,id));
CREATE INDEX IF NOT EXISTS object_names ON project_objects(snapshot,name);
CREATE INDEX IF NOT EXISTS object_parents ON project_objects(snapshot,parent);
CREATE TABLE IF NOT EXISTS object_indexes(snapshot TEXT PRIMARY KEY);
''')
            if 'recipe' not in {row['name'] for row in db.execute('PRAGMA table_info(object_indexes)')}:
                db.execute('ALTER TABLE object_indexes ADD COLUMN recipe TEXT'); db.commit()

    @contextmanager
    def project_connect(self):
        if not self.project_database.is_file(): raise ValueError('Индекс проекта ещё не создан.')
        db = sqlite3.connect(self.project_database.as_uri() + '?mode=ro', uri=True, timeout=30)
        db.row_factory = sqlite3.Row; db.create_function('casefold', 1, lambda value: (value or '').casefold())
        try: yield db
        finally: db.close()

    def projects(self):
        if not self.project_database.is_file(): return []
        with self.project_connect() as db:
            return [dict(row) for row in db.execute('SELECT p.id,p.name,p.active_snapshot AS snapshot,s.root FROM projects p JOIN snapshots s ON s.id=p.active_snapshot ORDER BY p.name')]

    def check_snapshot(self, snapshot):
        if not snapshot: raise ValueError('Выберите проект для поиска по его коду и метаданным.')
        with self.project_connect() as db:
            if not db.execute('SELECT 1 FROM snapshots WHERE id=?', (snapshot,)).fetchone():
                raise ValueError('Версия проекта не найдена.')

    def ensure_platform(self, source):
        with self.lock, self.reference.connect() as db:
            row = db.execute('SELECT * FROM sources WHERE id=?', (source,)).fetchone()
            if not row: raise ValueError('Выберите импортированную версию платформы.')
            ready = db.execute('SELECT fingerprint,recipe FROM language_versions WHERE source=?', (source,)).fetchone()
            if ready and ready['fingerprint'] == row['fingerprint'] and ready['recipe'] == 'symbols-v1': return
            db.execute('DELETE FROM platform_symbols WHERE source=?', (source,))
            for page in db.execute('SELECT p.id,p.source,p.book,p.path,p.title,p.sections,s.version FROM pages p JOIN sources s ON s.id=p.source WHERE p.source=?', (source,)):
                page = dict(page); page['sections'] = json.loads(page['sections'])
                record = platform_symbol(page)
                db.execute('INSERT INTO platform_symbols VALUES(?,?,?,?,?,?,?,?)', (record['id'], source, record['name'].casefold(), record['short_name'].casefold(), record['alias'].casefold(), record['alias'].rsplit('.', 1)[-1].casefold(), record['owner'].casefold(), json.dumps(record, ensure_ascii=False)))
            db.execute('INSERT OR REPLACE INTO language_versions VALUES(?,?,?)', (source, row['fingerprint'], 'symbols-v1')); db.commit()

    def ensure_objects(self, snapshot):
        self.check_snapshot(snapshot)
        with self.lock, self.reference.connect() as target:
            if snapshot in self.indexed_snapshots: return
            if target.execute("SELECT 1 FROM object_indexes WHERE snapshot=? AND recipe='objects-v2'", (snapshot,)).fetchone():
                self.indexed_snapshots.add(snapshot); return
            with self.project_connect() as project:
                rows = project.execute("SELECT node_id,json_extract(data,'$.name') AS name,json_extract(data,'$.kind') AS kind,json_extract(data,'$.label') AS label,json_extract(data,'$.properties.Synonym') AS synonym,json_extract(data,'$.parent') AS parent,json_extract(data,'$.source') AS path FROM metadata WHERE snapshot_id=?", (snapshot,)).fetchall()
            names = {row['node_id']: row['name'] for row in rows}
            for row in rows:
                record = {'id': row['node_id'], 'name': row['name'], 'origin': 'metadata', 'kind': row['kind'], 'owner': names.get(row['parent'], ''), 'owner_id': row['parent'], 'description': row['synonym'] or '', 'label': row['label'], 'signature': '', 'location': {'snapshot': snapshot, 'path': row['path']}}
                target.execute('INSERT OR REPLACE INTO project_objects VALUES(?,?,?,?,?,?)', (snapshot, row['node_id'], row['name'].casefold(), (row['synonym'] or '').casefold(), row['parent'], json.dumps(record, ensure_ascii=False)))
            target.execute('INSERT OR REPLACE INTO object_indexes VALUES(?,?)', (snapshot, 'objects-v2')); target.commit()
            self.indexed_snapshots.add(snapshot)

    @staticmethod
    def code_record(symbol, snapshot):
        return {'id': symbol['id'], 'origin': 'code', 'name': symbol['name'], 'owner': symbol['owner_name'], 'kind': symbol['kind'],
                'signature': symbol['name'] + '(' + symbol['parameters'] + ')',
                'parameters': symbol['parameter_details'], 'export': symbol['export'], 'syntax_valid': symbol['syntax_valid'],
                'directives': symbol.get('directives', []),
                'description': symbol['comment'], 'location': {'snapshot': snapshot, 'path': symbol['path'], 'line': symbol['declaration_line'], 'file_id': symbol['file_id'], 'file_hash': symbol['file_hash']}}

    def search(self, query, source='', snapshot='', scope='all', exact=False, limit=20):
        if not isinstance(query, str) or len(query) > 500: raise ValueError('Запрос должен быть строкой до 500 символов.')
        if scope not in {'all', 'platform', 'metadata', 'code'}: raise ValueError('Неизвестная область поиска.')
        query = query.strip(); limit = min(max(int(limit), 1), 50); groups = {}
        if not query: return {'query': query, 'groups': {}, 'source': source, 'snapshot': snapshot}
        if scope in {'all', 'platform'}:
            self.ensure_platform(source)
            with self.reference.connect() as db:
                rows = db.execute('SELECT record FROM platform_symbols WHERE source=? AND (name=? OR short_name=? OR alias=? OR short_alias=?) LIMIT ?', (source, *([query.casefold()] * 4), limit)).fetchall()
            matches = [json.loads(row[0]) for row in rows]
            if not exact and len(matches) < limit:
                found = {entry['id'] for entry in matches}
                for row in self.reference.search(query, source, limit):
                    if row['id'] not in found:
                        with self.reference.connect() as db: item = db.execute('SELECT record FROM platform_symbols WHERE id=?', (row['id'],)).fetchone()
                        matches.append(json.loads(item[0])); found.add(row['id'])
                    if len(matches) == limit: break
            groups['platform'] = matches
        if scope in {'metadata', 'code'}: self.check_snapshot(snapshot)
        if snapshot and scope in {'all', 'metadata', 'code'}:
            self.ensure_objects(snapshot)
            if scope in {'all', 'metadata'}:
                escaped = query.casefold().replace('!', '!!').replace('%', '!%').replace('_', '!_')
                with self.reference.connect() as db:
                    clause = 'name=?' if exact else "(name LIKE ? ESCAPE '!' OR synonym LIKE ? ESCAPE '!')"
                    args = [query.casefold()] if exact else ['%' + escaped + '%'] * 2
                    rows = db.execute('SELECT record FROM project_objects WHERE snapshot=? AND ' + clause + ' ORDER BY CASE WHEN name=? THEN 0 ELSE 1 END,name LIMIT ?', (snapshot, *args, query.casefold(), limit)).fetchall()
                groups['metadata'] = [json.loads(row[0]) for row in rows]
            if scope in {'all', 'code'}:
                if exact:
                    with self.project_connect() as db:
                        rows = db.execute('SELECT data FROM symbols WHERE snapshot_id=? AND casefold(name)=? LIMIT ?', (snapshot, query.casefold(), limit)).fetchall()
                    symbols = [json.loads(row[0]) for row in rows]
                else:
                    # Reuse canonical FTS without initializing/migrating the live project DB.
                    symbols = self.reader.search(snapshot, query, limit)
                groups['code'] = [self.code_record(symbol, snapshot) for symbol in symbols]
        return {'query': query, 'source': source, 'snapshot': snapshot, 'exact': exact, 'groups': groups}

    def members(self, owner, source, limit=100):
        if not isinstance(owner, str) or len(owner) > 500: raise ValueError('Недопустимый тип.')
        self.ensure_platform(source)
        with self.reference.connect() as db:
            return [json.loads(row[0]) for row in db.execute('SELECT record FROM platform_symbols WHERE source=? AND owner=? ORDER BY name LIMIT ?', (source, owner.casefold(), min(max(int(limit), 1), 500)))]

    def item(self, origin, identifier, source='', snapshot=''):
        if origin == 'platform':
            self.ensure_platform(source); page = self.reference.page(identifier)
            if page['source'] != source: raise ValueError('Статья относится к другой версии платформы.')
            return {**platform_symbol(page), 'body': page['body'], 'sections': page['sections']}
        self.check_snapshot(snapshot)
        if origin == 'metadata':
            with self.project_connect() as db:
                row = db.execute('SELECT data FROM metadata WHERE snapshot_id=? AND node_id=?', (snapshot, identifier)).fetchone()
                if not row: raise ValueError('Объект отсутствует в выбранной версии проекта.')
                node = json.loads(row[0])
                children = [json.loads(row[0]) for row in db.execute("SELECT data FROM metadata WHERE snapshot_id=? AND json_extract(data,'$.parent')=?", (snapshot, identifier))]
                modules = [dict(row) for row in db.execute('SELECT file_id,path,module_name FROM modules WHERE snapshot_id=? AND owner_id=?', (snapshot, identifier))]
            return {'origin': origin, 'id': identifier, 'name': node['name'], 'kind': node['kind'], 'body': json.dumps(node, ensure_ascii=False, indent=2), 'node': node, 'children': children, 'modules': modules, 'location': {'snapshot': snapshot, 'path': node.get('source', '')}}
        if origin == 'code':
            symbol = self.reader.symbol(snapshot, identifier)
            return {**self.code_record(symbol, snapshot), 'body': symbol['code'], 'incoming': symbol['incoming'], 'outgoing': symbol['outgoing']}
        raise ValueError('Неизвестный источник элемента.')
