"""Read-only catalog for the hierarchical XML/BSL Designer export format."""
from __future__ import annotations

import hashlib
from collections import defaultdict
import os
import xml.etree.ElementTree as ET
from pathlib import Path

LABELS = {
    'Configuration': 'Конфигурация', 'Subsystem': 'Подсистема',
    'CommonModule': 'Общий модуль', 'Catalog': 'Справочник', 'Document': 'Документ',
    'InformationRegister': 'Регистр сведений', 'AccumulationRegister': 'Регистр накопления',
    'AccountingRegister': 'Регистр бухгалтерии', 'CalculationRegister': 'Регистр расчёта',
    'Enum': 'Перечисление', 'Report': 'Отчёт', 'DataProcessor': 'Обработка',
    'ChartOfAccounts': 'План счетов', 'ChartOfCalculationTypes': 'План видов расчёта',
    'ChartOfCharacteristicTypes': 'План видов характеристик', 'ExchangePlan': 'План обмена',
    'BusinessProcess': 'Бизнес-процесс', 'Task': 'Задача', 'Constant': 'Константа',
    'Role': 'Роль', 'CommonForm': 'Общая форма', 'Form': 'Форма', 'Command': 'Команда',
    'CommonCommand': 'Общая команда', 'CommonTemplate': 'Общий макет', 'Template': 'Макет',
    'Attribute': 'Реквизит', 'StandardAttribute': 'Стандартный реквизит',
    'TabularSection': 'Табличная часть', 'Dimension': 'Измерение', 'Resource': 'Ресурс',
    'EnumValue': 'Значение перечисления', 'HTTPService': 'HTTP-сервис', 'WebService': 'Web-сервис',
    'WSReference': 'WS-ссылка', 'EventSubscription': 'Подписка на событие',
    'ScheduledJob': 'Регламентное задание', 'CommonAttribute': 'Общий реквизит',
    'DefinedType': 'Определяемый тип', 'SessionParameter': 'Параметр сеанса',
    'Language': 'Язык', 'Style': 'Стиль', 'StyleItem': 'Элемент стиля',
    'CommonPicture': 'Общая картинка', 'FilterCriterion': 'Критерий отбора',
    'DocumentJournal': 'Журнал документов', 'Sequence': 'Последовательность',
    'DocumentNumerator': 'Нумератор', 'CommandGroup': 'Группа команд',
    'FunctionalOption': 'Функциональная опция',
    'FunctionalOptionsParameter': 'Параметр функциональных опций',
    'SettingsStorage': 'Хранилище настроек', 'ExternalDataSource': 'Внешний источник данных',
    'IntegrationService': 'Сервис интеграции', 'Module': 'Модуль', 'File': 'Файл',
}
MODULES = {
    'Module': 'Модуль', 'ObjectModule': 'Модуль объекта', 'ManagerModule': 'Модуль менеджера',
    'RecordSetModule': 'Модуль набора записей', 'FormModule': 'Модуль формы',
    'CommandModule': 'Модуль команды', 'SessionModule': 'Модуль сеанса',
    'ManagedApplicationModule': 'Модуль управляемого приложения',
    'OrdinaryApplicationModule': 'Модуль обычного приложения',
    'ExternalConnectionModule': 'Модуль внешнего соединения',
}


def local(tag: str) -> str:
    return tag.rsplit('}', 1)[-1]


def child(element, name):
    return next((e for e in element if local(e.tag) == name), None)


def value(element) -> str:
    if element is None:
        return ''
    content = [t.strip() for t in element.itertext() if t.strip()]
    return ' · '.join(content)


def identifier(key: str) -> str:
    return hashlib.sha256(key.encode('utf-8')).hexdigest()[:24]


def canonical(text):
    return text.replace('\r\n', '\n').replace('\r', '\n')


def text_hash(text):
    return hashlib.sha256(canonical(text).encode('utf-8')).hexdigest()


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
        return raw.decode('utf-16')
    try:
        return raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        return raw.decode('cp1251')


def symbols(text: str) -> list[dict]:
    from .bsl_index import extract
    return [{'name': sym['name'], 'kind': sym['kind'], 'line': sym['declaration_line']}
            for sym in extract(text)['symbols']]


class Catalog:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.nodes: dict[str, dict] = {}
        self.files: dict[str, Path] = {}
        self.warnings: list[str] = []
        self.owners: dict[Path, str] = {}
        self.document_owners: dict[Path, str] = {}

    def add(self, key, name, kind, parent=None, source=None, properties=None, missing=False):
        node_id = identifier(key)
        self.nodes[node_id] = {
            'id': node_id, 'name': name, 'kind': kind, 'label': LABELS.get(kind, kind),
            'parent': parent, 'children': [], 'files': [], 'properties': properties or {},
            'source': source, 'missing': missing,
        }
        return node_id

    def parse_object(self, element, source, key, parent=None):
        kind = local(element.tag)
        props_element = child(element, 'Properties')
        props = {local(e.tag): value(e) for e in props_element} if props_element is not None else {}
        name = props.get('Name') or value(element) or kind
        node_id = self.add(key, name, kind, parent, source, props)
        children = child(element, 'ChildObjects')
        if children is not None:
            for number, nested in enumerate(children):
                if kind == 'Configuration':
                    continue  # Top-level references are reconciled with separate files below.
                self.parse_object(nested, source, f'{key}/{number}', node_id)
        return node_id

    def scan(self, progress=lambda phase, done, total: None):
        if not self.root.is_dir():
            raise ValueError('Каталог не существует. Выберите папку файловой выгрузки 1С.')
        if not (self.root / 'Configuration.xml').is_file():
            raise ValueError('В выбранной папке нет Configuration.xml. Выберите корень иерархической выгрузки конфигурации в XML/BSL, а не файл .cf или .dt.')
        paths = []
        for directory, dirs, filenames in os.walk(self.root, followlinks=False):
            dirs[:] = [d for d in dirs if d not in {'.git', '.athena', '.venv', 'node_modules'} and not (Path(directory) / d).is_symlink()]
            for name in filenames:
                path = Path(directory) / name
                if path.suffix.lower() in {'.xml', '.bsl', '.os'} and not path.is_symlink() and path.resolve().is_relative_to(self.root):
                    paths.append(path)
        xml_paths = sorted((p for p in paths if p.suffix.lower() == '.xml'), key=lambda p: (p.name != 'Configuration.xml', len(p.parts), str(p)))
        configuration = None
        references = []
        for number, path in enumerate(xml_paths, 1):
            progress('Чтение метаданных', number, len(xml_paths))
            rel = path.relative_to(self.root).as_posix()
            try:
                with path.open('rb') as stream:
                    first = next(ET.iterparse(stream, events=('start',)))[1]
                    if local(first.tag) != 'MetaDataObject':
                        continue
                tree = ET.parse(path).getroot()
                element = next((e for e in tree if child(e, 'Properties') is not None), None)
                if element is None:
                    continue
                node_id = self.parse_object(element, rel, rel)
                if path == self.root / 'Configuration.xml':
                    if local(element.tag) != 'Configuration':
                        raise ValueError('Configuration.xml не содержит объект Configuration.')
                    configuration = node_id
                    children = child(element, 'ChildObjects')
                    references = [(local(e.tag), value(e)) for e in children] if children is not None else []
                    self.owners[self.root] = node_id
                else:
                    self.owners[path.with_suffix('')] = node_id
                self.document_owners[path] = node_id
            except (ET.ParseError, OSError, UnicodeError) as exc:
                self.warnings.append(f'{rel}: {exc}')
        if not configuration:
            raise ValueError('Не удалось прочитать Configuration.xml. Проверьте формат выгрузки и корректность XML.')
        # Attach separate nested Form/Command/Subsystem definitions to their nearest owner.
        by_identity = defaultdict(list)
        children_by_parent = defaultdict(list)
        for node in self.nodes.values():
            if node['parent'] is not None:
                by_identity[(node['parent'], node['kind'], node['name'])].append(node['id'])
                children_by_parent[node['parent']].append(node['id'])
        progress('Связывание объектов', 0, len(self.document_owners))
        for number, (path, node_id) in enumerate(self.document_owners.items(), 1):
            if node_id == configuration:
                continue
            parent = next((self.owners[p] for p in path.parent.parents if p in self.owners), configuration)
            if path.parent in self.owners:
                parent = self.owners[path.parent]
            node = self.nodes[node_id]
            node['parent'] = parent
            placeholders = by_identity.pop((parent, node['kind'], node['name']), [])
            for placeholder_id in placeholders:
                if placeholder_id == node_id or placeholder_id not in self.nodes:
                    continue
                for nested_id in children_by_parent.pop(placeholder_id, []):
                    nested = self.nodes[nested_id]
                    nested['parent'] = node_id
                    children_by_parent[node_id].append(nested_id)
                    by_identity[(node_id, nested['kind'], nested['name'])].append(nested_id)
                del self.nodes[placeholder_id]
            if number % 128 == 0 or number == len(self.document_owners):
                progress('Связывание объектов', number, len(self.document_owners))
        existing = {(n['kind'], n['name']) for n in self.nodes.values() if n['parent'] == configuration}
        for kind, name in references:
            if (kind, name) not in existing:
                self.add(f'missing/{kind}/{name}', name, kind, configuration, missing=True)
                self.warnings.append(f'{LABELS.get(kind, kind)} «{name}»: XML объекта отсутствует в выгрузке.')
        for number, path in enumerate(paths, 1):
            progress('Связывание файлов', number, len(paths))
            rel = path.relative_to(self.root).as_posix()
            owner = self.document_owners.get(path)
            if owner is None:
                owner = next((self.owners[p] for p in (path.parent, *path.parent.parents) if p in self.owners), configuration)
            file_id = identifier('file/' + rel)
            self.files[file_id] = path
            file_info = {'id': file_id, 'path': rel, 'name': path.name, 'language': 'bsl' if path.suffix.lower() in {'.bsl', '.os'} else 'xml'}
            self.nodes[owner]['files'].append(file_info)
            if file_info['language'] == 'bsl':
                label = MODULES.get(path.stem, path.stem)
                if path.stem == 'Module' and self.nodes[owner]['kind'] in {'Form', 'CommonForm'}:
                    label = 'Модуль формы'
                module_id = self.add('module/' + rel, label, 'Module', owner, rel)
                self.nodes[module_id]['files'].append(file_info)
        for node in self.nodes.values():
            if node['parent'] in self.nodes:
                self.nodes[node['parent']]['children'].append(node['id'])
        for node in self.nodes.values():
            node['children'].sort(key=lambda i: (self.nodes[i]['label'], self.nodes[i]['name'].casefold()))
            node['files'].sort(key=lambda f: (f['language'] != 'bsl', f['path']))
        self.configuration = configuration
        return self

    def summary(self):
        return {
            'root': str(self.root), 'name': self.nodes[self.configuration]['name'],
            'configuration': self.configuration, 'nodes': list(self.nodes.values()),
            'warnings': self.warnings,
            'stats': {'objects': sum(n['kind'] not in {'Module'} for n in self.nodes.values()),
                      'modules': sum(p.suffix.lower() in {'.bsl', '.os'} for p in self.files.values()),
                      'files': len(self.files)},
        }

    def file(self, file_id):
        path = self.files.get(file_id)
        if not path:
            raise ValueError('Файл не найден в текущем проекте.')
        if not path.resolve().is_relative_to(self.root):
            raise ValueError('Файл находится за пределами проекта.')
        text = read_text(path)
        return {'path': path.relative_to(self.root).as_posix(), 'content': text,
                'hash': text_hash(text), 'editable': path.suffix.lower() == '.bsl',
                'symbols': symbols(text) if path.suffix.lower() in {'.bsl', '.os'} else []}
