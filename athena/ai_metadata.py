"""Snapshot-backed answers to configuration inventory questions."""
import re
from athena.catalog import LABELS


KINDS = {
    'Document': ('Документы', r'\bдокумент\w*'),
    'Catalog': ('Справочники', r'\bсправочник\w*'),
    'CommonModule': ('Общие модули', r'\bобщ\w*\s+модул\w*'),
    'InformationRegister': ('Регистры сведений', r'\bрегистр\w*\s+сведени\w*'),
    'AccumulationRegister': ('Регистры накопления', r'\bрегистр\w*\s+накоплени\w*'),
    'AccountingRegister': ('Регистры бухгалтерии', r'\bрегистр\w*\s+бухгалтери\w*'),
    'CalculationRegister': ('Регистры расчёта', r'\bрегистр\w*\s+расчет\w*'),
    'Enum': ('Перечисления', r'\bперечислени\w*'),
    'Report': ('Отчёты', r'\bотчет\w*'),
    'DataProcessor': ('Обработки', r'\bобработк(?:а|и|у|е|ой|ам|ами|ах)\b'),
    'Constant': ('Константы', r'\bконстант\w*'),
    'Subsystem': ('Подсистемы', r'\bподсистем\w*'),
    'Role': ('Роли', r'\bрол(?:ь|и|ей|ям|ями|ях)\b'),
}


def structure_request(question):
    """Broad structural counts only; named-object reasoning stays with tools."""
    text = question[:500].casefold().replace('ё', 'е')
    fields = {'Attribute': r'реквизит', 'TabularSection': r'табличн\w*',
              'Dimension': r'измерени', 'Resource': r'ресурс'}
    matched = [kind for kind, pattern in fields.items() if re.search(pattern, text)]
    if not matched or not re.search(r'\b(?:сколько|количеств\w*)\b', text):
        return None
    if re.search(r'стандартн|общ\w*\s+реквизит|тип\w*|строков\w*|числов\w*|ссылоч\w*', text):
        return None
    if 'Attribute' in matched and 'TabularSection' in matched and not re.search(r'реквизит\w*\s+и\s+табличн|табличн\w*(?:\s+част\w*)?\s+и\s+реквизит',text):
        return None
    if not re.search(r'\b(?:кажд\w*|всех|все|справочниках|справочников|документах|документов|регистрах|регистров)\b', text):
        return None
    kinds = [kind for kind, (_, pattern) in KINDS.items() if re.search(pattern, text)]
    return ({'kinds': kinds, 'field': matched[0]} if len(matched)==1 else {'kinds': kinds,'fields':matched}) if kinds else None


def inventory_request(question):
    """Only broad list/count questions; code and per-object questions keep RAG."""
    text = question[:500].casefold().replace('ё', 'е')
    if re.search(r'\b(?:код\w*|метод\w*|функци\w*|процедур\w*|обработчик\w*|'
                 r'использу\w*|вызыва\w*|реквизит\w*|табличн\w*|измерени\w*|ресурс\w*|содерж\w*|запрос\w*|'
                 r'провед\w*|печата\w*|созда\w*|связан\w*)\b', text):
        return None
    if re.search(r'\b(?:покажи\w*|показать)\s+(?:мне\s+)?'
                 r'(?:документ|справочник|отчет|обработку|общий\s+модуль)\b', text):
        return None
    listing = re.search(r'\b(?:какие|какой|сколько|перечисли\w*|список|перечень|'
                        r'покажи\w*|показать|все|структура|состав)\b', text)
    kinds = [kind for kind, (_, pattern) in KINDS.items() if re.search(pattern, text)]
    if re.search(r'\bжурнал\w*\s+документ\w*', text):
        return None
    if not listing:
        # A bare category name is also a useful inventory request.
        bare = re.sub(r'[^\w\s]', '', text).strip()
        if bare not in {label.casefold().replace('ё', 'е') for label, _ in KINDS.values()}:
            return None
    if kinds:
        return kinds
    if listing and re.search(r'\b(?:объект\w*|структура|состав)\b', text):
        return []
    if listing and re.search(r'\bрегистр\w*', text):
        return [kind for kind in KINDS if kind.endswith('Register')]
    return None


def inventory_answer(inventory):
    sources = []
    for item in inventory['objects']:
        label = LABELS.get(item['kind'], item['kind'])
        details = f"Вид: {label}\nИмя: {item['name']}"
        synonym = item.get('properties', {}).get('Synonym')
        if synonym:
            details += '\nСиноним: ' + synonym
        path = item.get('source') or 'Configuration.xml'
        details += '\nИсточник метаданных: ' + path
        if ('field' in inventory or 'fields' in inventory) and item.get('child_counts') is not None:
            details += '\nЧисло непосредственных дочерних объектов: ' + str(item['child_counts'])
        if item.get('missing'):
            details += '\nОбъект объявлен в конфигурации, но его XML отсутствует в выгрузке.'
        sources.append({'type': 'metadata', 'id': item['id'], 'object_id': item['id'],
                        'name': item['name'], 'kind': item['kind'], 'path': path,
                        'label': f'S{len(sources)+1}', 'reason': 'metadata', 'code': details})
    requested = inventory['requested_kinds']
    if len(requested) == 1:
        heading = KINDS[requested[0]][0] + ': ' + str(inventory['total'])
    else:
        heading = 'Объекты метаданных: ' + str(inventory['total'])
    lines = [heading, 'По метаданным текущей сохранённой конфигурации.', '']
    fields = inventory.get('fields') or ([inventory['field']] if inventory.get('field') else [])
    if fields:
        titles = [LABELS.get(field,field) for field in fields]
        title = ' и '.join(titles)
        lines = [f'## {title}: количество по объектам', '',
                 'По XML текущей сохранённой конфигурации. Считаются только непосредственные дочерние объекты.', '']
        if 'Attribute' in fields:
            lines += ['Это собственные реквизиты объекта. Стандартные и общие реквизиты, а также реквизиты табличных частей в это число не входят.', '']
        lines += ['| Объект | '+' | '.join(t+': количество' for t in titles)+' | Источник |',
                  '| --- | '+' | '.join('---:' for _ in fields)+' | --- |']
        for item, source in zip(inventory['objects'], sources):
            count = ' | '.join(str(item['child_counts'].get(field, 0)) if item.get('child_counts') is not None else 'XML отсутствует' for field in fields)
            name = item['name'].replace('|', '\\|').replace('\n', ' ')
            lines.append(f"| {name} | {count} | [{source['label']}] |")
        if not sources:
            lines += ['', 'Объекты выбранных видов в этой конфигурации не обнаружены.']
    elif not sources:
        lines.append('Объекты выбранных видов в этой конфигурации не обнаружены.')
    for index, source in enumerate([] if fields else sources, 1):
        name = source['name']
        if len(requested) != 1:
            name = LABELS.get(source['kind'], source['kind']) + ': ' + name
        lines.append(f"{index}. {name} [{source['label']}]")
    if not inventory['complete']:
        lines.extend(['', f"Показаны первые {len(sources)} из {inventory['total']} объектов. "
                      'Полный список доступен в дереве конфигурации; уточните вид объектов.'])
    return '\n'.join(lines), sources
