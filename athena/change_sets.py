"""Persistent reviewable changes; preparing a package never modifies an export."""
import difflib
import json
import shutil
import threading
import time
import uuid
from pathlib import Path
from xml.dom import minidom as DOM
import xml.etree.ElementTree as ET
from athena.catalog import read_text, text_hash, canonical, child, local
from athena.bsl_index import extract
from athena.objects import prepare_object, normalize_spec, digest, validate_stage, object_import_command, TYPE_ORDER
from athena.editing import run_import


class ChangeSets:
    def __init__(self, directory):
        self.directory = Path(directory).resolve() / 'change-sets'; self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    def folder(self, identity):
        if not isinstance(identity, str) or len(identity) != 32 or any(c not in '0123456789abcdef' for c in identity): raise ValueError('Некорректный идентификатор набора.')
        folder = self.directory / identity
        if folder.resolve() != folder: raise ValueError('Недопустимый путь набора.')
        return folder

    def read(self, identity):
        with self.lock:
            try: return json.loads((self.folder(identity) / 'manifest.json').read_text(encoding='utf-8'))
            except OSError as exc: raise ValueError('Набор изменений не найден.') from exc

    def write(self, value):
        folder = self.folder(value['id']); folder.mkdir(exist_ok=True)
        value['updated'] = time.time()
        temp = folder / 'manifest.tmp'; temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8'); temp.replace(folder / 'manifest.json')

    def list(self, project):
        with self.lock:
            values = []
            for path in self.directory.glob('*/manifest.json'):
                try:
                    value = self.read(path.parent.name)
                    if value['project'] == project: values.append({key: value[key] for key in ['id', 'title', 'status', 'revision', 'updated']} | {'count': sum(e['enabled'] for e in value['entries']), 'owner_login':value.get('owner_login'), 'owner':value.get('owner'), 'revoked':value.get('revoked',False)})
                except (ValueError, KeyError): continue
            return sorted(values, key=lambda value: -value['updated'])

    def create(self, project, title):
        if not isinstance(title, str) or not title.strip() or len(title) > 120: raise ValueError('Введите название набора до 120 символов.')
        value = {'id': uuid.uuid4().hex, 'title': title.strip(), 'project': project, 'status': 'draft', 'revision': 1, 'entries': [], 'history': [], 'package': None}
        from athena.accounts import ACTOR
        actor=ACTOR.get() or {};value.update(owner=actor.get('id'),owner_login=actor.get('login'))
        with self.lock: self.write(value)
        return value

    def editable(self, identity, project, revision):
        value = self.read(identity)
        from athena.accounts import require_owner
        require_owner(value.get('owner'))
        if value.get('revoked'):raise ValueError('Набор отменён главным программистом.')
        if type(revision) is not int: raise ValueError('Укажите версию набора.')
        if value['project'] != project: raise ValueError('Набор относится к другому проекту.')
        if value['revision'] != revision: raise ValueError('Набор изменён в другом окне. Обновите список.')
        if value['status'] in {'submitting', 'applied', 'needs_review', 'archived'}: raise ValueError('Этот набор нельзя редактировать. Создайте новый набор или восстановите архивный.')
        return value

    def mutate(self, identity, project, revision, action):
        with self.lock:
            value = self.editable(identity, project, revision); action(value)
            if len(value['entries']) > 100 or len(json.dumps(value, ensure_ascii=False).encode()) > 25_000_000: raise ValueError('Лимит набора: 100 изменений и 25 МБ.')
            value['revision'] += 1; value['status'] = 'draft'; value['package'] = None
            value['history'].append({'revision': value['revision'], 'time': time.time()}); self.write(value)
            return value

    def add(self, identity, project, revision, catalog, entry):
        if entry.get('kind') == 'module':
            file_id = entry.get('file_id'); content = entry.get('content'); base = entry.get('base_hash')
            source = catalog.files.get(file_id) if isinstance(file_id, str) else None
            if not source or source.suffix.lower() != '.bsl' or not source.resolve().is_relative_to(catalog.root): raise ValueError('Выберите BSL-модуль текущего проекта.')
            if not isinstance(content, str) or len(content.encode()) > 2_000_000 or '\x00' in content: raise ValueError('Недопустимый текст модуля.')
            if not isinstance(base, str) or len(base) != 64: raise ValueError('Укажите исходный хеш модуля.')
            path = source.relative_to(catalog.root).as_posix()
            record = {'id': 'module:' + file_id, 'kind': 'module', 'file_id': file_id, 'path': path, 'content': canonical(content), 'base_hash': base, 'enabled': True}
            record['before_content']=canonical(read_text(source))
        elif entry.get('kind') == 'object':
            spec = normalize_spec(catalog, entry.get('spec', {}))
            record = {'id': 'object:' + spec['kind'] + ':' + spec['name'].casefold(), 'kind': 'object', 'spec': spec, 'path': spec['kind'] + '.' + spec['name'], 'base_hash': text_hash(read_text(catalog.root / 'Configuration.xml')), 'enabled': True}
        else: raise ValueError('Неизвестный вид изменения.')
        def action(value):
            old = next((index for index, item in enumerate(value['entries']) if item['id'] == record['id']), None)
            if old is None: value['entries'].append(record)
            else: value['entries'][old] = record
        return self.mutate(identity, project, revision, action)

    def enable(self, identity, project, revision, entry_id, enabled):
        if type(enabled) is not bool: raise ValueError('Укажите включение изменения.')
        def action(value):
            entry = next((e for e in value['entries'] if e['id'] == entry_id), None)
            if entry is None: raise ValueError('Изменение не найдено.')
            entry['enabled'] = enabled
        return self.mutate(identity, project, revision, action)

    def validate(self, value, catalog):
        if value['status'] == 'archived': raise ValueError('Восстановите набор из архива.')
        checks = []; active = [e for e in value['entries'] if e['enabled']]
        existing = {(n['kind'], n['name'].casefold()) for n in catalog.nodes.values()}
        config_hash = text_hash(read_text(catalog.root / 'Configuration.xml'))
        for entry in active:
            errors = []; warnings = []; difference = ''
            if entry['kind'] == 'module':
                source = catalog.files.get(entry['file_id'])
                if not source or not source.is_file() or not source.resolve().is_relative_to(catalog.root): errors.append('Модуль отсутствует в текущем проекте.')
                else:
                    before = read_text(source)
                    if source.relative_to(catalog.root).as_posix() != entry['path']: errors.append('Путь модуля изменился.')
                    if text_hash(before) != entry['base_hash']: errors.append('Исходный модуль изменился после создания черновика.')
                    if canonical(before) == canonical(entry['content']): errors.append('В модуле нет изменений.')
                    difference = ''.join(difflib.unified_diff(canonical(before).splitlines(keepends=True), entry['content'].splitlines(keepends=True), fromfile=entry['path'], tofile=entry['path']))
                warnings = extract(entry['content'])['warnings']
            else:
                spec = entry['spec']
                if (spec['kind'], spec['name'].casefold()) in existing: errors.append('Объект с таким именем уже существует.')
                if config_hash != entry['base_hash']: errors.append('Configuration.xml изменился после добавления объекта в набор.')
                for name in ['object_module', 'manager_module']:
                    warnings.extend({**w, 'module': name} for w in extract(spec[name])['warnings'])
                difference = json.dumps(spec, ensure_ascii=False, indent=2)
            if warnings: errors.append('В BSL обнаружены ошибки разбора. Исправьте код перед подготовкой.')
            checks.append({'id': entry['id'], 'path': entry['path'], 'errors': errors, 'diagnostics': warnings[:100], 'diff': difference[:200000], 'truncated': len(difference) > 200000})
        return {'valid': bool(active) and not any(c['errors'] for c in checks), 'checks': checks, 'revision': value['revision'], 'message': '' if active else 'Включите хотя бы одно изменение.'}

    def prepare(self, identity, project, revision, catalog, profile):
        with self.lock:
            value = self.editable(identity, project, revision); validation = self.validate(value, catalog)
            if not validation['valid']: return {'prepared': False, **validation}
            package_id = uuid.uuid4().hex; folder = self.folder(identity) / 'packages' / package_id; stage = folder / 'files'; stage.mkdir(parents=True)
            before = catalog.root / 'Configuration.xml'; (folder / 'Configuration.before.xml').write_bytes(before.read_bytes())
            document = DOM.parseString(before.read_bytes()); configuration = next(n for n in document.documentElement.childNodes if n.nodeType == n.ELEMENT_NODE and n.localName == 'Configuration')
            children = next(n for n in configuration.childNodes if n.nodeType == n.ELEMENT_NODE and n.localName == 'ChildObjects')
            external = catalog.root / 'Ext'
            if external.is_dir(): shutil.copytree(external, stage / 'Ext')
            selected = ['Configuration.xml']; objects = []; module_hashes = {}; baselines = {}
            for entry in value['entries']:
                if not entry['enabled']: continue
                if entry['kind'] == 'module':
                    target = stage / entry['path']; target.parent.mkdir(parents=True, exist_ok=True); target.write_text(entry['content'], encoding='utf-8-sig')
                    backup = folder / 'before' / entry['path']; backup.parent.mkdir(parents=True, exist_ok=True); backup.write_bytes(catalog.files[entry['file_id']].read_bytes())
                    selected.append(entry['path']); module_hashes[entry['path']] = text_hash(entry['content']); baselines[entry['file_id']] = entry['base_hash']
                else:
                    draft, _ = prepare_object(catalog, entry['spec'], profile, folder / 'object-builds')
                    objects.append({'spec': draft['spec'], 'path': draft['path'], 'folder': draft['folder']})
                    for relative in draft['selected'][1:]:
                        target = stage / relative; target.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(Path(draft['folder']) / 'files' / relative, target); selected.append(relative)
                    spec = draft['spec']; reference = document.createElementNS(configuration.namespaceURI, spec['kind']); reference.appendChild(document.createTextNode(spec['name']))
                    peers = [n for n in children.childNodes if n.nodeType == n.ELEMENT_NODE and n.localName == spec['kind']]
                    anchor = peers[-1].nextSibling if peers else next((n for n in children.childNodes if n.nodeType == n.ELEMENT_NODE and n.localName in TYPE_ORDER and TYPE_ORDER.index(n.localName) > TYPE_ORDER.index(spec['kind'])), None)
                    children.insertBefore(reference, anchor)
            (stage / 'Configuration.xml').write_bytes(document.toxml(encoding='utf-8') if objects else before.read_bytes())
            hashes = {p.relative_to(stage).as_posix(): digest(p) for p in stage.rglob('*') if p.is_file()}
            package = {'id': package_id, 'operation': 'change_set', 'set_id': identity, 'set_revision': revision, 'root': str(catalog.root), 'connection': profile['id'] if profile else None, 'path': value['title'], 'folder': str(folder), 'selected': selected, 'hashes': hashes, 'base_hash': text_hash(read_text(before)), 'baselines': baselines, 'module_hashes': module_hashes, 'objects': objects, 'status': 'prepared'}
            (folder / 'manifest.json').write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding='utf-8')
            (folder / 'review.json').write_text(json.dumps(validation['checks'], ensure_ascii=False), encoding='utf-8')
            value['status'] = 'prepared'; value['package'] = package; self.write(value)
            return {'prepared': True, 'id': package_id, 'set': value, 'selected': selected, 'folder': str(folder), 'checks': validation['checks'], 'can_upload': bool(profile)}

    def archive(self, identity, project, revision, restore=False):
        if type(restore) is not bool: raise ValueError('Некорректный режим восстановления.')
        with self.lock:
            value = self.read(identity)
            from athena.accounts import require_owner
            require_owner(value.get('owner'))
            if restore and value.get('revoked'):raise ValueError('Отменённый набор нельзя восстановить; создайте новый.')
            if value['project'] != project or type(revision) is not int or value['revision'] != revision: raise ValueError('Проект или версия набора изменились.')
            if value['status'] in {'submitting', 'needs_review'}: raise ValueError('Сначала проверьте результат загрузки в 1С.')
            if restore:
                if value['status'] != 'archived': raise ValueError('Набор не находится в архиве.')
                value['status'] = 'applied' if value.get('previous_status') == 'applied' else 'draft'
                if value['status'] == 'draft': value['package'] = None
            else:
                value['previous_status'] = value['status']; value['status'] = 'archived'
            value['revision'] += 1; self.write(value); return value

    def prepared(self, package_id):
        for path in self.directory.glob('*/manifest.json'):
            value = self.read(path.parent.name); package = value.get('package')
            if package and package['id'] == package_id: return package
        return None

    def review(self, value):
        package = value.get('package')
        if not package or value['status'] != 'prepared': raise ValueError('Сначала подготовьте пакет.')
        expected = self.folder(value['id']) / 'packages' / package['id']
        self.folder(package['id'])
        if expected.resolve() != expected or Path(package['folder']).resolve() != expected: raise ValueError('Недопустимый путь пакета.')
        return json.loads((expected / 'review.json').read_text(encoding='utf-8'))

    def check_package(self, package, catalog):
        value = self.read(package['set_id'])
        from athena.accounts import require_owner
        require_owner(value.get('owner'))
        self.folder(package['id'])
        expected = self.folder(package['set_id']) / 'packages' / package['id']
        if Path(package['folder']).resolve() != expected or expected.resolve() != expected: raise ValueError('Недопустимый путь пакета.')
        if value['revision'] != package['set_revision'] or value['status'] != 'prepared': raise ValueError('Набор изменён после подготовки. Подготовьте его снова.')
        if text_hash(read_text(catalog.root / 'Configuration.xml')) != package['base_hash']: raise ValueError('Configuration.xml изменился после подготовки.')
        for file_id, expected_hash in package['baselines'].items():
            if file_id not in catalog.files or text_hash(read_text(catalog.files[file_id])) != expected_hash: raise ValueError('Исходный модуль изменился после подготовки набора.')
        validate_stage(package)
        if any(relative not in package['hashes'] or Path(relative).is_absolute() or '..' in Path(relative).parts for relative in package['selected']): raise ValueError('Недопустимый список файлов для загрузки.')

    def mark(self, package, status):
        with self.lock:
            value = self.read(package['set_id']); value['status'] = status
            if value.get('package'): value['package']['status'] = status
            self.write(value)


def upload_set(profile, password, package, progress):
    validate_stage(package); folder = Path(package['folder'])
    run_import(object_import_command(profile, password, package, folder / 'import.log', folder / 'result.txt'), password, folder / 'import.log', folder / 'result.txt', progress, 'Загрузка набора изменений в конфигурацию 1С', 300)


def verify_set(catalog, package):
    stage = Path(package['folder']) / 'files'
    validate_stage(package)
    for relative in package['selected']:
        if relative.endswith('.bsl'):
            source = catalog.root / relative
            if not source.is_file() or text_hash(read_text(source)) != text_hash(read_text(stage / relative)): raise ValueError('Код после загрузки не совпал: ' + relative)
    for obj in package['objects']:
        actual_path = catalog.root / obj['path']
        if not actual_path.is_file(): raise ValueError('После загрузки отсутствует объект: ' + obj['path'])
        actual = child(ET.parse(actual_path).getroot(), obj['spec']['kind']); expected = child(ET.parse(stage / obj['path']).getroot(), obj['spec']['kind'])
        def shape(element):
            return (element.tag, tuple(sorted(element.attrib.items())), (element.text or '').strip(), tuple(shape(c) for c in element))
        if actual is None or shape(actual) != shape(expected): raise ValueError('Свойства созданного объекта отличаются от пакета: ' + obj['path'])
    original = child(ET.parse(Path(package['folder']) / 'Configuration.before.xml').getroot(), 'Configuration')
    final = child(ET.parse(catalog.root / 'Configuration.xml').getroot(), 'Configuration')
    refs = lambda node: {(local(c.tag), (c.text or '').strip()) for c in child(node, 'ChildObjects')}
    expected_refs = refs(original) | {(obj['spec']['kind'], obj['spec']['name']) for obj in package['objects']}
    if refs(final) != expected_refs: raise ValueError('Список объектов после загрузки отличается от ожидаемого.')
    def settings(element):
        return (element.tag, tuple(sorted(element.attrib.items())), (element.text or '').strip(), tuple(settings(c) for c in element if local(c.tag) != 'ChildObjects'))
    if settings(original) != settings(final): raise ValueError('Свойства конфигурации после загрузки изменились.')
