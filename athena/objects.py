"""Build and review new Catalog/Document metadata without changing a snapshot."""
from __future__ import annotations

import difflib
import hashlib
import json
import re
import shutil
import time
import uuid
from pathlib import Path
from xml.dom import minidom as DOM
import xml.etree.ElementTree as ET

from athena.catalog import child, local, read_text, text_hash
from athena.editing import run_import
from athena.exporter import command

TEMPLATES = Path(__file__).parent / 'templates'
KINDS = {'Catalog': 'Catalogs', 'Document': 'Documents'}
PRIMITIVES = {'String': 'xs:string', 'Number': 'xs:decimal', 'Boolean': 'xs:boolean', 'Date': 'xs:dateTime'}
RESERVED = {'ссылка','пометкаудаления','ref','deletionmark'}
TYPE_ORDER = ('Language Subsystem StyleItem Style CommonPicture SessionParameter Role CommonTemplate '
              'FilterCriterion CommonModule CommonAttribute ExchangePlan XDTOPackage WebService HTTPService WSReference '
              'EventSubscription ScheduledJob SettingsStorage FunctionalOption FunctionalOptionsParameter DefinedType '
              'CommonCommand CommandGroup Constant CommonForm Catalog Document DocumentNumerator Sequence DocumentJournal '
              'Enum Report DataProcessor InformationRegister AccumulationRegister ChartOfCharacteristicTypes ChartOfAccounts '
              'AccountingRegister ChartOfCalculationTypes CalculationRegister BusinessProcess Task ExternalDataSource IntegrationService').split()


def name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[^\W\d]\w{0,79}', value, re.UNICODE):
        raise ValueError('Имя: до 80 букв, цифр и подчёркиваний; первый символ — буква или подчёркивание.')
    if re.fullmatch(r'CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]',value,re.I):
        raise ValueError('Это имя зарезервировано операционной системой.')
    return value


def text(value, limit=500):
    if not isinstance(value, str) or len(value)>limit or any(ord(c)<32 and c not in '\n\t' for c in value):
        raise ValueError('Некорректный текст свойства.')
    return value


def integer(value, minimum, maximum):
    if type(value) is not int or not minimum<=value<=maximum:
        raise ValueError(f'Параметр типа должен быть целым числом от {minimum} до {maximum}.')
    return value


def normalize_spec(catalog, data):
    if not isinstance(data, dict) or data.get('kind') not in KINDS:
        raise ValueError('Выберите справочник или документ.')
    kind = data['kind']; object_name = name(data.get('name'))
    if any(n['kind']==kind and n['name'].casefold()==object_name.casefold() for n in catalog.nodes.values()):
        raise ValueError('Объект с таким именем уже существует.')
    if any(p.stem.casefold()==object_name.casefold() for p in (catalog.root/KINDS[kind]).glob('*.xml')):
        raise ValueError('Файл объекта с таким именем уже существует.')
    references = {f"{n['kind']}Ref.{n['name']}" for n in catalog.nodes.values() if n['kind'] in KINDS}
    references.add(f'{kind}Ref.{object_name}')
    def attributes(rows, reserved):
        if not isinstance(rows,list) or len(rows)>100:
            raise ValueError('Допускается до 100 реквизитов в одной группе.')
        result=[]; seen=set(reserved)
        for row in rows:
            if not isinstance(row,dict):raise ValueError('Некорректный реквизит.')
            field=name(row.get('name'));key=field.casefold()
            if key in seen:raise ValueError(f'Повторяющееся или стандартное имя реквизита: {field}.')
            seen.add(key);typ=row.get('type','String')
            if typ not in PRIMITIVES and typ not in references:raise ValueError('Тип реквизита отсутствует в проекте.')
            item={'name':field,'synonym':text(row.get('synonym','')),'type':typ}
            if typ=='String':item['length']=integer(row.get('length',100),0,4096)
            if typ=='Number':
                item['digits']=integer(row.get('digits',15),1,38)
                item['scale']=integer(row.get('scale',2),0,item['digits'])
            result.append(item)
        return result
    reserved=RESERVED | ({'код','наименование','родитель','это группа','code','description','parent','isfolder'} if kind=='Catalog' else {'номер','дата','проведен','number','date','posted'})
    attrs=attributes(data.get('attributes',[]),reserved)
    sections=data.get('sections',[])
    if not isinstance(sections,list) or len(sections)>20:raise ValueError('Допускается до 20 табличных частей.')
    seen=reserved | {a['name'].casefold() for a in attrs};tabs=[]
    for section in sections:
        if not isinstance(section,dict):raise ValueError('Некорректная табличная часть.')
        field=name(section.get('name'))
        if field.casefold() in seen:raise ValueError('Имя табличной части уже используется.')
        seen.add(field.casefold())
        tabs.append({'name':field,'synonym':text(section.get('synonym','')),
                     'attributes':attributes(section.get('attributes',[]),{'номерстроки','linenumber'})})
    return {'kind':kind,'name':object_name,'synonym':text(data.get('synonym','')),'attributes':attrs,'sections':tabs,
            'object_module':text(data.get('object_module',''),1_000_000),'manager_module':text(data.get('manager_module',''),1_000_000)}


def elements(node):
    return [n for n in node.childNodes if n.nodeType==n.ELEMENT_NODE]


def direct(node, tag):
    return next((n for n in elements(node) if n.localName==tag),None)


def clear(node):
    for n in list(node.childNodes):node.removeChild(n)


def set_text(node, value):
    clear(node)
    if value!='':node.appendChild(node.ownerDocument.createTextNode(str(value)))


def append(node, tag, value=''):
    document=node.ownerDocument
    element=document.createElementNS('http://v8.1c.ru/8.1/data/core' if tag.startswith('v8:') else node.namespaceURI,tag)
    node.appendChild(element);set_text(element,value)
    return element


def identity(node, kind, object_name, section=None):
    node.setAttribute('uuid',str(uuid.uuid4()))
    for element in node.getElementsByTagName('*'):
        if element.localName in {'TypeId','ValueId'}:set_text(element,str(uuid.uuid4()))
        if element.localName=='GeneratedType':
            prefix=element.getAttribute('name').split('.')[0]
            if section:prefix=prefix.replace('Document',kind).replace('Catalog',kind)
            element.setAttribute('name',f'{prefix}.{object_name}'+(f'.{section}' if section else ''))


def properties(node, spec, language):
    props=direct(node,'Properties');set_text(direct(props,'Name'),spec['name'])
    synonym=direct(props,'Synonym');clear(synonym)
    if spec['synonym']:
        item=append(synonym,'v8:item');append(item,'v8:lang',language);append(item,'v8:content',spec['synonym'])
    return props


def build_xml(spec, version, language):
    doc=DOM.parse(str(TEMPLATES/(spec['kind']+'.xml')))
    doc.documentElement.setAttribute('version',version)
    obj=direct(doc.documentElement,spec['kind']);identity(obj,spec['kind'],spec['name']);props=properties(obj,spec,language)
    for field in props.getElementsByTagName('xr:Field'):
        set_text(field,''.join(n.data for n in field.childNodes if n.nodeType==n.TEXT_NODE).replace('.Template.','.'+spec['name']+'.'))
    children=direct(obj,'ChildObjects')
    def attribute(row, parent):
        template=DOM.parse(str(TEMPLATES/'Attribute.xml'))
        attr=doc.importNode(direct(template.documentElement,'Attribute'),True);parent.appendChild(attr)
        identity(attr,spec['kind'],spec['name']);p=properties(attr,row,language);typ=direct(p,'Type');clear(typ)
        append(typ,'v8:Type',PRIMITIVES.get(row['type'],'cfg:'+row['type']))
        if row['type']=='String':
            q=append(typ,'v8:StringQualifiers');append(q,'v8:Length',row['length']);append(q,'v8:AllowedLength','Variable')
        elif row['type']=='Number':
            q=append(typ,'v8:NumberQualifiers');append(q,'v8:Digits',row['digits']);append(q,'v8:FractionDigits',row['scale']);append(q,'v8:AllowedSign','Any')
        elif row['type']=='Date':
            q=append(typ,'v8:DateQualifiers');append(q,'v8:DateFractions','DateTime')
    for row in spec['attributes']:attribute(row,children)
    for section in spec['sections']:
        template=DOM.parse(str(TEMPLATES/'TabularSection.xml'))
        tab=doc.importNode(direct(template.documentElement,'TabularSection'),True);children.appendChild(tab)
        identity(tab,spec['kind'],spec['name'],section['name']);properties(tab,section,language)
        for row in section['attributes']:attribute(row,direct(tab,'ChildObjects'))
    return doc.toxml(encoding='utf-8').decode('utf-8')


def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare_object(catalog, data, profile, directory):
    if profile and profile.get('extension'):
        raise ValueError('Создание объектов в расширениях пока не поддерживается. Откройте основную конфигурацию.')
    spec=normalize_spec(catalog,data)
    source=catalog.root/'Configuration.xml';document=DOM.parse(str(source));configuration=direct(document.documentElement,'Configuration')
    if not configuration:raise ValueError('Не найдена конфигурация в Configuration.xml.')
    children=direct(configuration,'ChildObjects')
    if children is None:raise ValueError('В Configuration.xml отсутствует список объектов.')
    before=read_text(source);language='ru'
    languages=[n['properties'].get('LanguageCode','') for n in catalog.nodes.values() if n['kind']=='Language']
    if languages:language=next((c for c in languages if c=='ru'),next((c for c in languages if c),'ru'))
    reference=document.createElementNS(configuration.namespaceURI,spec['kind']);reference.appendChild(document.createTextNode(spec['name']))
    # Preserve type ordering; Catalog comes immediately before Document.
    peers=[n for n in elements(children) if n.localName==spec['kind']]
    anchor=peers[-1].nextSibling if peers else next((n for n in elements(children) if n.localName in TYPE_ORDER and
                                                   TYPE_ORDER.index(n.localName)>TYPE_ORDER.index(spec['kind'])),None)
    children.insertBefore(reference,anchor)
    identity_id=uuid.uuid4().hex;folder=Path(directory).resolve()/'objects'/identity_id;stage=folder/'files';stage.mkdir(parents=True)
    (folder/'Configuration.before.xml').write_bytes(source.read_bytes())
    (stage/'Configuration.xml').write_bytes(document.toxml(encoding='utf-8'))
    # Root external properties include support settings. Preserve their bytes.
    external=catalog.root/'Ext'
    if external.is_dir():shutil.copytree(external,stage/'Ext')
    xml_path=f"{KINDS[spec['kind']]}/{spec['name']}.xml";target=stage/xml_path;target.parent.mkdir(parents=True)
    version=document.documentElement.getAttribute('version') or '2.21'
    target.write_text(build_xml(spec,version,language),encoding='utf-8-sig')
    modules=[]
    for key,module in [('object_module','ObjectModule'),('manager_module','ManagerModule')]:
        path=f"{KINDS[spec['kind']]}/{spec['name']}/Ext/{module}.bsl";p=stage/path;p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text(spec[key],encoding='utf-8-sig');modules.append(path)
    hashes={p.relative_to(stage).as_posix():digest(p) for p in stage.rglob('*') if p.is_file()}
    draft={'id':identity_id,'operation':'create_object','root':str(catalog.root),'connection':profile['id'] if profile else None,
           'folder':str(folder),'path':xml_path,'base_hash':text_hash(before),'spec':spec,'hashes':hashes,
           'selected':['Configuration.xml',xml_path,*modules],'status':'prepared','created':time.time()}
    (folder/'manifest.json').write_text(json.dumps(draft,ensure_ascii=False,indent=2),encoding='utf-8')
    difference=''.join(difflib.unified_diff(before.splitlines(keepends=True),read_text(stage/'Configuration.xml').splitlines(keepends=True),fromfile='Configuration.xml',tofile='Configuration.xml'))
    for path in [xml_path,*modules]:
        difference+=f'\n+++ {path} (новый файл)\n'+''.join('+'+line+'\n' for line in read_text(stage/path).splitlines())
    return draft,{'id':identity_id,'path':xml_path,'connection':profile['name'] if profile else None,'extension':profile.get('extension','') if profile else '',
                  'diff':difference[:200_000],'truncated':len(difference)>200_000,'backup':str(folder/'Configuration.before.xml'),
                  'folder':str(folder),'selected':draft['selected'],'spec':spec,'can_upload':bool(profile)}


def validate_stage(draft):
    stage=Path(draft['folder'])/'files'
    if stage.is_symlink() or any(p.is_symlink() or not p.resolve().is_relative_to(stage.resolve()) for p in stage.rglob('*')):
        raise ValueError('Пакет объекта содержит внешние ссылки. Подготовьте его заново.')
    actual={p.relative_to(stage).as_posix():digest(p) for p in stage.rglob('*') if p.is_file()}
    if actual!=draft['hashes']:
        raise ValueError('Пакет объекта изменился. Подготовьте его заново.')


def object_import_command(profile,password,draft,log,result):
    folder=Path(draft['folder']);stage=folder/'files';listing=folder/'load-list.txt'
    listing.write_text('\n'.join(str(stage/p) for p in draft['selected']),encoding='utf-8-sig')
    args=command(profile,password,stage,log,result);start=args.index('/DumpConfigToFiles');end=args.index('/Out')
    operation=['/LoadConfigFromFiles',str(stage),'-Format','Hierarchical','-listFile',str(listing)]
    if profile.get('extension'):operation+=['-Extension',profile['extension']]
    return args[:start]+operation+args[end:]


def upload_object(profile,password,draft,progress=lambda **kwargs:None):
    validate_stage(draft);folder=Path(draft['folder']);log=folder/'import.log';result=folder/'result.txt'
    run_import(object_import_command(profile,password,draft,log,result),password,log,result,progress,
               'Добавление объекта в конфигурацию 1С',300)


def verify_object(catalog,draft):
    spec=draft['spec'];path=catalog.root/draft['path']
    if not path.is_file():raise ValueError('В новой выгрузке отсутствует созданный объект.')
    actual=ET.parse(path).getroot();obj=child(actual,spec['kind'])
    expected=child(ET.parse(Path(draft['folder'])/'files'/draft['path']).getroot(),spec['kind'])
    if obj is None or obj.get('uuid')!=expected.get('uuid'):raise ValueError('Идентификатор созданного объекта не совпал.')
    def shape(element):
        props=child(element,'Properties');nested=child(element,'ChildObjects');typ=child(props,'Type')
        synonym=child(props,'Synonym')
        return (local(element.tag),child(props,'Name').text,tuple(t.strip() for t in synonym.itertext() if t.strip()) if synonym is not None else (),
                tuple((local(e.tag),(e.text or '').strip()) for e in typ.iter()) if typ is not None else (),
                tuple(shape(e) for e in nested if local(e.tag) in {'Attribute','TabularSection'}) if nested is not None else ())
    if shape(obj)!=shape(expected):raise ValueError('Реквизиты созданного объекта не совпали с подготовленным пакетом.')
    for relative in draft['selected'][2:]:
        p=catalog.root/relative
        if not p.is_file() or text_hash(read_text(p))!=text_hash(read_text(Path(draft['folder'])/'files'/relative)):
            raise ValueError('Код созданного объекта не совпал с подготовленным пакетом.')
    # Root import must preserve the complete original object list.
    original=child(ET.parse(Path(draft['folder'])/'Configuration.before.xml').getroot(),'Configuration')
    final=child(ET.parse(catalog.root/'Configuration.xml').getroot(),'Configuration')
    def refs(root):return {(local(n.tag),(n.text or '').strip()) for n in child(root,'ChildObjects')}
    if refs(final)!=refs(original)|{(spec['kind'],spec['name'])}:
        raise ValueError('Список объектов конфигурации после загрузки отличается от ожидаемого.')
    def settings(node):
        return (local(node.tag),tuple(sorted(node.attrib.items())),(node.text or '').strip(),
                tuple(settings(n) for n in node if local(n.tag)!='ChildObjects'))
    if settings(original)!=settings(final):
        raise ValueError('Свойства конфигурации после добавления объекта изменились. Проверьте результат в Конфигураторе.')
