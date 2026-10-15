"""Conservative, fully matched creation requests with no model interpretation.

Anything outside this grammar stays with the model, including additional
requirements, questions, negation and multiple actions. Never discard a tail.
"""
import re

from athena.objects import name


IDENTIFIER = r'[^\W\d]\w{0,79}'
OBJECT_NAME = rf'(?:«(?P<guillemet>{IDENTIFIER})»|"(?P<quoted>{IDENTIFIER})"|(?P<plain>{IDENTIFIER}))'
CREATE = re.compile(
    rf'(?:пожалуйста[, ]+)?(?:создай(?:те)?|добавь(?:те)?|создать|добавить)\s+'
    rf'(?:(?:новый|пустой)\s+)?(?P<kind>справочник|документ)\s+'
    rf'(?:(?:с именем|под названием)\s+)?{OBJECT_NAME}'
    rf'(?P<fields>\s+(?:без\s+(?:собственных\s+|дополнительных\s+)?реквизитов|с\s+реквизит(?:ом|ами)\s+.+))?'
    rf'[.!]?\s*', re.IGNORECASE)
FIELD = re.compile(
    rf'(?P<name>{IDENTIFIER})'
    r'(?:\s*(?::|\s+типа\s+|\s+тип\s+)\s*'
    r'(?P<type>Строка|String|Число|Number|Булево|Boolean|Дата|Date)'
    r'(?:\s*\(\s*(?P<size>\d+)\s*(?:,\s*(?P<scale>\d+)\s*)?\))?)?', re.IGNORECASE)
TYPES = {'строка':'String','string':'String','число':'Number','number':'Number',
         'булево':'Boolean','boolean':'Boolean','дата':'Date','date':'Date'}
NUMERIC_NAMES = {'цена','сумма','количество','стоимость','скидка','процент','вес','остаток'}


def default_type(field_name):
    """Visible creation defaults; the user reviews all inferred types in Changes."""
    key=field_name.casefold().replace('ё','е')
    if key in NUMERIC_NAMES:return 'Number'
    if key in {'дата','датасоздания','датаизменения'}:return 'Date'
    if key in {'активен','активный','включен'}:return 'Boolean'
    return 'String'


def creation_spec(goal):
    """Parse one explicit Catalog/Document creation or return None unchanged."""
    match=CREATE.fullmatch(goal.strip())
    if not match:return None
    object_name=match['guillemet'] or match['quoted'] or match['plain']
    try:name(object_name)
    except ValueError:return None
    spec={'kind':'Catalog' if match['kind'].casefold()=='справочник' else 'Document',
          'name':object_name,'attributes':[],'sections':[]}
    fields=(match['fields'] or '').strip().rstrip('.!').strip()
    if not fields or fields.casefold().startswith('без '):return spec
    fields=re.sub(r'^с\s+реквизит(?:ом|ами)\s+','',fields,flags=re.IGNORECASE)
    # Commas inside Number(15,2) are not separators between attributes.
    rows=re.split(r'\s*(?:[;,]|\s+и\s+)\s*(?![^()]*\))',fields)
    seen=set()
    for row in rows:
        field=FIELD.fullmatch(row)
        if not field or field['name'].casefold() in seen:return None
        seen.add(field['name'].casefold())
        typ=TYPES[field['type'].casefold()] if field['type'] else default_type(field['name'])
        attribute={'name':field['name'],'type':typ}
        if field['type'] is None and typ=='Number':attribute.update(digits=15,scale=2)
        size=int(field['size']) if field['size'] else None
        scale=int(field['scale']) if field['scale'] else None
        if typ=='String':
            if scale is not None or (size is not None and not 0<=size<=4096):return None
            if size is not None:attribute['length']=size
        elif typ=='Number':
            if size is not None:
                if not 1<=size<=38 or (scale is not None and not 0<=scale<=size):return None
                attribute.update(digits=size,scale=scale if scale is not None else 0)
        elif size is not None:return None
        spec['attributes'].append(attribute)
    return spec if len(spec['attributes'])<=100 else None
