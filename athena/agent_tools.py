"""Provider-neutral agent tools. Writes only stage reviewable change sets."""
import hashlib
import json
import re
import sqlite3
import threading
import time
from collections import Counter
from contextlib import contextmanager
from athena.catalog import LABELS, canonical, read_text, text_hash
from athena.evidence import read_window


def string(limit=500): return {'type': 'string', 'minLength': 1, 'maxLength': limit}
def integer(low, high): return {'type': 'integer', 'minimum': low, 'maximum': high}
def obj(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required), 'additionalProperties': False}
def array(items, maximum): return {'type': 'array', 'items': items, 'maxItems': maximum}


ATTRIBUTE = obj({'name': string(80), 'synonym': {'type': 'string', 'maxLength': 500}, 'type': string(120),
                 'length': integer(0, 4096), 'digits': integer(1, 38), 'scale': integer(0, 38)}, ['name'])
SPEC = obj({'kind': {'type': 'string', 'enum': ['Catalog', 'Document']}, 'name': string(80),
            'synonym': {'type': 'string', 'maxLength': 500}, 'attributes': array(ATTRIBUTE, 100),
            'sections': array(obj({'name': string(80), 'synonym': {'type': 'string', 'maxLength': 500},
                                  'attributes': array(ATTRIBUTE, 100)}, ['name']), 20),
            'object_module': {'type': 'string', 'maxLength': 1000000},
            'manager_module': {'type': 'string', 'maxLength': 1000000}}, ['kind', 'name'])
SET = {'set_id': string(32), 'revision': integer(0, 1000000000)}
PAGE = {'offset': integer(0, 10000000), 'limit': integer(1, 100)}


def definitions():
    rows = [
        ('get_configuration', 'Корень конфигурации и полный счётчик объектов каждого вида. Начни с этого инструмента для обзора проекта.', obj({}), False),
        ('list_objects', 'Полный постраничный список объектов конфигурации. child_counts считает собственные реквизиты Attribute, табличные части TabularSection, измерения Dimension и ресурсы Resource напрямую из XML, независимо от наличия BSL. Стандартные и общие реквизиты и поля табличных частей не входят в Attribute. null означает отсутствующий XML. Для полного списка пройди все next_offset.', obj({'kinds': array({'type':'string','enum':list(LABELS)}, 20), **PAGE}), False),
        ('search', 'Гибридный поиск по коду, метаданным, справке, документам и Git. exact=true сохраняет точный поиск имени метода. mode показывает фактически выполненный поиск.', obj({'query': string(), 'scope': {'type':'string','enum':['all','code','metadata','platform','documents','git']}, 'source': string(128), 'exact': {'type':'boolean'}, 'limit': integer(1, 30)}, ['query']), False),
        ('read_module', 'Читать диапазон BSL и хеш для правки. Для продолжения передай next_line и next_column как start_line и start_column. set_id читает черновик.', obj({'file_id':string(128), 'set_id':string(32), 'start_line':integer(1,1000000), 'start_column':integer(0,1000000), 'line_count':integer(1,300)}, ['file_id']), False),
        ('read_document', 'Дочитать документ или Git-источник проекта по id из поиска; продолжение next_line/next_column.', obj({'document_id':string(4096), 'start_line':integer(1,1000000), 'start_column':integer(0,1000000), 'line_count':integer(1,300)}, ['document_id']), False),
        ('get_object', 'Читать объект метаданных, список модулей и страницу дочерних объектов.', obj({'object_id':string(128), **PAGE}, ['object_id']), False),
        ('get_symbol', 'Читать функцию или процедуру из зафиксированного снимка проекта.', obj({'symbol_id':string(128)}, ['symbol_id']), False),
        ('get_calls', 'Страница входящих или исходящих вызовов; неразрешённые связи отмечены явно.', obj({'symbol_id':string(128), 'direction':{'type':'string','enum':['incoming','outgoing']}, **PAGE}, ['symbol_id','direction']), False),
        ('read_reference', 'Читать статью синтаксис-помощника; дочитывать next_line/next_column.', obj({'page_id':string(256), 'source':string(128),'start_line':integer(1,1000000),'start_column':integer(0,1000000),'line_count':integer(1,300)}, ['page_id','source']), False),
        ('list_change_sets', 'Список наборов текущего проекта.', obj(PAGE), False),
        ('read_change_set', 'Состав и версия набора без полного текста модулей.', obj({'set_id':string(32)}, ['set_id']), False),
        ('create_change_set', 'Создать отдельный набор предложений для проверки пользователем.', obj({'title':string(120)}, ['title']), True),
        ('edit_module', 'Последовательные точные замены в BSL. Каждая old_text должна встретиться ровно один раз. expected_hash относится к текущему тексту черновика или исходнику.', obj({**SET, 'file_id':string(128), 'expected_hash':string(64), 'edits':{**array(obj({'old_text':string(200000), 'new_text':{'type':'string','maxLength':200000}}, ['old_text','new_text']), 30), 'minItems':1}}, [*SET,'file_id','expected_hash','edits']), True),
        ('create_object', 'Добавить в набор новый справочник или документ с реквизитами, табличными частями и модулями.', obj({**SET,'spec':SPEC}, [*SET,'spec']), True),
        ('update_new_object', 'Исправить спецификацию уже предложенного нового объекта. Имя и вид остаются прежними; существующие объекты 1С не изменяются.', obj({**SET,'entry_id':string(256),'spec':SPEC}, [*SET,'entry_id','spec']), True),
        ('set_change_enabled', 'Включить или исключить изменение из пакета.', obj({**SET,'entry_id':string(256),'enabled':{'type':'boolean'}}, [*SET,'entry_id','enabled']), True),
        ('validate_changes', 'Проверить синтаксис BSL и конфликты исходников; это не проверка платформой 1С.', obj(SET, SET), False),
        ('show_diff', 'Читать разницу одного изменения и локальные диагностики.', obj({**SET,'entry_id':string(256)}, [*SET,'entry_id']), False),
        ('prepare_changes', 'Собрать пакет для просмотра в Правках. Загрузка в 1С требует отдельного действия пользователя.', obj(SET, SET), True),
    ]
    return [{'type':'function','function':{'name':name,'description':description,'parameters':schema}, 'effect':'stage' if write else 'read'} for name,description,schema,write in rows]


def validate(value, schema, path='arguments'):
    typ = schema['type']
    valid = {'object':isinstance(value,dict), 'array':isinstance(value,list), 'string':isinstance(value,str),
             'integer':type(value) is int, 'boolean':type(value) is bool}[typ]
    if not valid: raise ValueError(path + ': ожидается ' + typ)
    if 'enum' in schema and value not in schema['enum']: raise ValueError(path + ': недопустимое значение')
    if typ == 'object':
        if set(value) - set(schema['properties']): raise ValueError(path + ': неизвестные параметры')
        if set(schema['required']) - set(value): raise ValueError(path + ': отсутствуют обязательные параметры')
        for key, item in value.items(): validate(item, schema['properties'][key], path+'.'+key)
    elif typ in {'array','string'}:
        minimum, maximum = ('minItems','maxItems') if typ=='array' else ('minLength','maxLength')
        if not schema.get(minimum,0) <= len(value) <= schema.get(maximum,1000000): raise ValueError(path + ': превышен допустимый размер')
        if typ=='array':
            for item in value: validate(item,schema['items'],path+'[]')
    elif typ=='integer' and not schema['minimum'] <= value <= schema['maximum']: raise ValueError(path + ': значение вне диапазона')


def bounded(value):
    """Bound individual fields; paging and the final envelope cap bound whole replies."""
    if isinstance(value,str): return value[:16000]
    if isinstance(value,list): return [bounded(v) for v in value[:100]]
    if isinstance(value,dict): return {k:bounded(v) for k,v in value.items()}
    return value


class AgentTools:
    def __init__(self, workspace, directory):
        self.workspace = workspace; self.lock = threading.RLock()
        self.path = directory / 'agent-tools.sqlite3'
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS receipts (id TEXT PRIMARY KEY, fingerprint TEXT, result TEXT, created REAL, tool TEXT, status TEXT)')

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path)
        try:
            with db: yield db
        finally: db.close()

    def context(self):
        w = self.workspace
        with w.lock:
            return self._context()

    def _context(self):
        w = self.workspace
        return {'project': w.active_connection or (str(w.catalog.root) if w.catalog else None),
                'snapshot':w.knowledge_snapshot, 'workspace_revision':w.revision,
                'ready':bool(w.catalog and w.knowledge_snapshot and w.job['status']!='running'),
                'sources':[{'id':s['id'],'version':s['version']} for s in w.language.reference.sources()],
                'writes':'change_sets_only', 'upload_requires_user':True}

    def history(self):
        with self.connect() as db:
            return [dict(zip(['call_id','tool','status','created'],row)) for row in db.execute('SELECT id,tool,status,created FROM receipts ORDER BY created DESC LIMIT 50')]

    def call(self, body):
        """One serialized call; durable receipts make successful mutation retries idempotent."""
        try:
            if not isinstance(body,dict) or set(body)!={'call_id','tool','context','arguments'}: raise ValueError('Укажите call_id, tool, context и arguments.')
            validate(body['call_id'], string(128), 'call_id')
            validate(body['context'], obj({'project':string(2048),'snapshot':string(128),'workspace_revision':integer(0,1000000000)}, ['project','snapshot','workspace_revision']), 'context')
            definition=next((d for d in definitions() if d['function']['name']==body['tool']),None)
            if not definition: raise ValueError('Неизвестный инструмент.')
            validate(body['arguments'],definition['function']['parameters'])
            fingerprint=hashlib.sha256(json.dumps(body,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
            with self.lock, self.workspace.lock:
                context=self._context()
                if not context['ready'] or any(body['context'][k]!=context[k] for k in body['context']):
                    return {'ok':False,'error':{'code':'stale_context','message':'Проект обновился или занят. Получите новый контекст.'},'context':context}
                with self.connect() as db:
                    old=db.execute('SELECT fingerprint,result FROM receipts WHERE id=?',(body['call_id'],)).fetchone()
                    if old:
                        if old[0]!=fingerprint: raise ValueError('call_id уже использован с другими аргументами.')
                        return json.loads(old[1]) | {'replayed':True}
                    if body['tool']=='search':
                        # Search reads immutable snapshots; it must not block UI state or cancellation.
                        self.workspace.lock.release()
                        try:result=self._dispatch(body['tool'],body['arguments'],context)
                        finally:self.workspace.lock.acquire()
                        current=self._context()
                        if not current['ready'] or any(body['context'][k]!=current[k] for k in body['context']):
                            return {'ok':False,'error':{'code':'stale_context','message':'Проект изменился во время поиска. Повторите запрос.'},'context':current}
                    else:result=self._dispatch(body['tool'],body['arguments'],context)
                    encoded=json.dumps(result,ensure_ascii=False)
                    if len(encoded.encode())>250000:
                        result={'truncated':True,'preview':encoded[:16000],'message':'Уточните запрос или используйте меньшую страницу.'}
                    response={'ok':True,'result':result,'context':context}
                    db.execute('INSERT INTO receipts VALUES (?,?,?,?,?,?)',(body['call_id'],fingerprint,json.dumps(response,ensure_ascii=False),time.time(),body['tool'],'ok'))
                    # Cache contains bounded tool replies, never credentials. Keep a finite replay window.
                    db.execute('DELETE FROM receipts WHERE id IN (SELECT id FROM receipts ORDER BY created DESC LIMIT -1 OFFSET 1000)')
                    return response
        except (ValueError,UnicodeError) as exc:
            return {'ok':False,'error':{'code':'invalid_request','message':str(exc)}}
        except (OSError,sqlite3.Error,RuntimeError) as exc:
            return {'ok':False,'error':{'code':'tool_failure','message':'Сбой инструмента ('+type(exc).__name__+'). Повторите запрос или проверьте доступность индекса.'}}

    @staticmethod
    def summary(value):
        result={k:value[k] for k in ['id','title','status','revision']}
        result['entries']=[{k:v for k,v in entry.items() if k not in {'content','spec'}} | ({'spec':{k:v for k,v in entry['spec'].items() if k not in {'object_module','manager_module'}}} if 'spec' in entry else {}) | ({'hash':text_hash(entry['content'])} if 'content' in entry else {}) for entry in value['entries']]
        return result

    def _dispatch(self, tool, a, context):
        w=self.workspace; catalog=w.catalog; snapshot=context['snapshot']; project=context['project']; store=w.change_sets
        def get_set():
            value=store.read(a['set_id'])
            if value['project']!=project: raise ValueError('Набор относится к другому проекту.')
            if 'revision' in a and a['revision']!=value['revision']: raise ValueError('Версия набора изменилась. Прочитайте набор заново.')
            return value
        def page(rows):
            offset=a.get('offset',0); limit=a.get('limit',30)
            return {'items':bounded(rows[offset:offset+limit]),'total':len(rows),'next_offset':offset+limit if offset+limit<len(rows) else None}
        if tool=='get_configuration':
            root=next(n for n in catalog.nodes.values() if n['kind']=='Configuration')
            counts=Counter(n['kind'] for n in catalog.nodes.values() if n.get('parent')==root['id'] and n['kind'] not in {'Module','File'})
            return {'id':root['id'],'name':root['name'],'source':root['source'],
                    'object_counts':dict(counts),'total':sum(counts.values()),'snapshot':snapshot}
        if tool=='list_objects':
            result=w.knowledge.metadata_inventory(snapshot,a.get('kinds',[]),a.get('limit',20),a.get('offset',0))
            return {'items':[{k:n.get(k) for k in ['id','name','kind','source','missing','child_counts']} for n in result['objects']],
                    'total':result['total'],'next_offset':result['next_offset'],
                    'count_scope':'Непосредственные дочерние объекты XML; Attribute — только собственные реквизиты. Стандартные, общие и поля табличных частей не включены.'}
        if tool=='search':
            scope=a.get('scope','code')
            if scope=='git':return w.git_projects.search(context['project'],a['query'],a.get('limit',5))
            if scope=='documents':return w.reference_context(context['project'],a['query'],a.get('limit',5))
            result=bounded(w.language.search(a['query'],source=a.get('source',''),snapshot=snapshot,scope=scope,exact=a.get('exact',False),limit=a.get('limit',20)))
            result.update(mode='exact' if a.get('exact') else 'lexical', requested_mode='exact' if a.get('exact') else 'hybrid')
            if scope in {'code','all'} and not a.get('exact'):
                found=w.search_functions(a['query'],'hybrid')
                result.update(mode=found['mode'],warning=found['warning'])
                result.setdefault('groups',{})['code']=[w.language.code_record(row,snapshot) | {k:row[k] for k in ('match','similarity','rank_score') if k in row} for row in found['results'][:a.get('limit',20)]]
            # Give the agent actual evidence on the first search, without another model round trip.
            for hit in result.get('groups',{}).get('code',[])[:3]:
                with w.knowledge.connect() as db:
                    row=db.execute('SELECT data FROM symbols WHERE snapshot_id=? AND id=?',(snapshot,hit['id'])).fetchone()
                if row:
                    symbol=json.loads(row[0]);lines=symbol.get('code','').splitlines()
                    terms=[t.casefold() for t in re.findall(r'\w+',a['query']) if len(t)>2]
                    match=next((i for i,line in enumerate(lines) if any(t in line.casefold() for t in terms)),0)
                    start=max(0,match-3)
                    window=read_window('\n'.join(lines),start+1,24,max_chars=1400)
                    for key in ('start_line','end_line','next_line'):
                        if window.get(key) is not None:window[key]+=symbol['start_line']-1
                    window['truncated']=window['partial']
                    hit['code_excerpt']=window
            if scope=='all':result['documents']=w.reference_context(context['project'],a['query'],min(a.get('limit',5),5))
            return result
        if tool=='read_document':
            item=(w.git_projects.read_snapshot(project,a['document_id']) if ':' in a['document_id'] else w.document_knowledge.read(project,a['document_id']))
            if not item['enabled']:raise ValueError('Документ исключён из контекста проекта.')
            return {k:item[k] for k in ('id','name','hash')} | read_window(item['content'],a.get('start_line',1),a.get('line_count',100),a.get('start_column',0))
        if tool=='read_reference':
            item=w.language.item('platform',a['page_id'],source=a['source'])
            window=read_window(item.pop('body',''),a.get('start_line',1),a.get('line_count',100),a.get('start_column',0))
            item['section_names']=list(item.pop('sections',{}))
            item.update(window)
            item['body']=item.pop('content')
            return {'item':bounded(item),'truncated':window['partial']}
        if tool=='get_object':
            node=catalog.nodes.get(a['object_id'])
            if not node: raise ValueError('Объект отсутствует в текущем проекте.')
            children=[n for n in catalog.nodes.values() if n.get('parent')==a['object_id']]
            return {'node':bounded(node),'children':page(children),
                    'child_counts':None if node.get('missing') else dict(Counter(n['kind'] for n in children))}
        if tool in {'get_symbol','get_calls'}:
            with w.knowledge.connect() as db:
                row=db.execute('SELECT data FROM symbols WHERE snapshot_id=? AND id=?',(snapshot,a['symbol_id'])).fetchone()
                if not row: raise ValueError('Метод отсутствует в текущем снимке.')
                if tool=='get_symbol':
                    item=json.loads(row[0]); item.pop('calls',None)
                    window=read_window(item.pop('code',''),1,1000000)
                    offset=item['start_line']-1
                    for key in ('start_line','end_line','next_line'):
                        if window.get(key) is not None:window[key]+=offset
                    item['symbol_end_line']=item['end_line']
                    item.update({k:v for k,v in window.items() if k!='content'},code=window['content'])
                    return {'item':bounded(item),'truncated':window['partial'],
                            'continuation':{'tool':'read_module','file_id':item['file_id'],'start_line':window['next_line'],'start_column':window['next_column']} if window['next_line'] else None}
                incoming=a['direction']=='incoming'; key='target_symbol' if incoming else 'source_symbol'
                total=db.execute(f'SELECT count(*) FROM calls WHERE snapshot_id=? AND {key}=?',(snapshot,a['symbol_id'])).fetchone()[0]
                offset=a.get('offset',0); limit=a.get('limit',30)
                rows=db.execute(f'SELECT source_symbol,name,receiver,line,target_symbol,resolution FROM calls WHERE snapshot_id=? AND {key}=? ORDER BY line,source_symbol,name LIMIT ? OFFSET ?',(snapshot,a['symbol_id'],limit,offset))
                return {'items':[dict(r) for r in rows],'total':total,'next_offset':offset+limit if offset+limit<total else None}
        if tool=='list_change_sets': return page(store.list(project))
        if tool=='create_change_set': return self.summary(store.create(project,a['title']))
        if tool=='read_change_set': return self.summary(get_set())
        if tool=='read_module' or tool=='edit_module':
            source=catalog.files.get(a['file_id'])
            if not source or source.suffix.lower()!='.bsl' or not source.resolve().is_relative_to(catalog.root): raise ValueError('Выберите BSL-модуль текущего проекта.')
            original=canonical(read_text(source)); entry=None
            if 'set_id' in a: entry=next((e for e in get_set()['entries'] if e.get('file_id')==a['file_id']),None)
            content=entry['content'] if entry else original
            if tool=='read_module':
                return {'file_id':a['file_id'],'path':source.relative_to(catalog.root).as_posix(),'hash':text_hash(content),'base_hash':text_hash(original),'draft':bool(entry)} | read_window(content,a.get('start_line',1),a.get('line_count',100),a.get('start_column',0))
            if text_hash(content)!=a['expected_hash']: raise ValueError('Хеш текста изменился. Прочитайте модуль заново.')
            if entry and entry['base_hash']!=text_hash(original): raise ValueError('Исходник изменился после создания черновика.')
            for edit in a['edits']:
                if content.count(edit['old_text'])!=1: raise ValueError('old_text должна встречаться ровно один раз; правка не применена.')
                content=content.replace(edit['old_text'],edit['new_text'],1)
            return self.summary(store.add(a['set_id'],project,a['revision'],catalog,{'kind':'module','file_id':a['file_id'],'base_hash':text_hash(original),'content':content}))
        value=get_set()
        if tool in {'create_object','update_new_object'}:
            key='object:'+a['spec']['kind']+':'+a['spec']['name'].casefold()
            old=next((e for e in value['entries'] if e['id']==key),None)
            if tool=='create_object' and old: raise ValueError('Объект уже добавлен в набор. Используйте update_new_object.')
            if tool=='update_new_object':
                if not old or a['entry_id']!=key or old['spec']['name']!=a['spec']['name']: raise ValueError('Выберите предложенный новый объект с тем же именем и видом.')
                if text_hash(read_text(catalog.root/'Configuration.xml'))!=old['base_hash']: raise ValueError('Конфигурация изменилась после добавления объекта.')
            return self.summary(store.add(a['set_id'],project,a['revision'],catalog,{'kind':'object','spec':a['spec']}))
        if tool=='set_change_enabled': return self.summary(store.enable(a['set_id'],project,a['revision'],a['entry_id'],a['enabled']))
        if tool=='show_diff':
            check=next((c for c in store.validate(value,catalog)['checks'] if c['id']==a['entry_id']),None)
            if not check: raise ValueError('Изменение отсутствует или исключено из пакета.')
            check['truncated']=check['truncated'] or len(check['diff'])>16000
            return bounded(check)
        if tool=='validate_changes': return bounded(store.validate(value,catalog))
        if tool=='prepare_changes':
            profile=next((p for p in w.profiles.all() if p['id']==w.active_connection),None)
            result=store.prepare(a['set_id'],project,a['revision'],catalog,profile)
            if not result['prepared']: return bounded(result)
            w.drafts[result['id']]=result['set']['package']
            return {'prepared':True,'package_id':result['id'],'set':self.summary(result['set']),'selected':result['selected'],'requires_user_review':True,'can_upload':result['can_upload'],'review_location':'Правки → Наборы изменений'}
        raise ValueError('Неизвестный инструмент.')
