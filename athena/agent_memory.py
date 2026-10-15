"""Bounded factual memory for tool runs, independent of rolling chat history."""
import json
import copy
from athena.evidence import key_fragments, clip_window


STABLE_READS = {'get_configuration', 'list_objects', 'get_object', 'get_symbol',
                'get_calls', 'read_reference', 'read_module', 'read_document', 'search'}


def node_summary(node):
    result = {k:node[k] for k in ['id','name','kind','path','source','missing','child_counts'] if k in node}
    properties = node.get('properties',{})
    if properties:
        result['properties'] = {k:str(properties[k])[:1200] for k in
            ['Synonym','Type','Length','Digits','FractionDigits','Hierarchical','HierarchyType','UseStandardCommands'] if k in properties}
    if node.get('files'):
        result['files'] = [{k:f[k] for k in ['id','name','path','kind'] if k in f} for f in node['files'][:20]]
    return result


def observation(tool, arguments, response):
    result = response['result']
    if tool == 'get_object':
        children = result['children']
        result = {'node':node_summary(result['node']), 'child_counts':result.get('child_counts'),
                  'children':{**{k:v for k,v in children.items() if k != 'items'},
                              'items':[node_summary(n) for n in children['items']]}}
    elif tool in {'read_module','read_document'}:
        result = {k:v for k,v in result.items() if k != 'content'}
        result['excerpt'] = response['result']['content'][:1800]
        result['memory_excerpt_truncated']=len(response['result']['content'])>1800
        if result['memory_excerpt_truncated']:
            result['fragments']=key_fragments(response['result']['content'],result.get('start_line',1),start_column=result.get('start_column',0))
            result.pop('excerpt')
    elif tool == 'get_symbol':
        item = result.get('item', {})
        result = {'item':{k:v for k,v in item.items() if k in
                  {'id','name','path','file_id','file_hash','kind','declaration_line','start_line','end_line','signature'}}}
        result['item']['excerpt']=item.get('code','')[:1800]
        result['item']['memory_excerpt_truncated']=len(item.get('code',''))>1800
        result['item']['next_line']=item.get('next_line')
        result['item']['next_column']=item.get('next_column',0)
        if result['item']['memory_excerpt_truncated']:
            result['item']['fragments']=key_fragments(item.get('code',''),item.get('start_line',1))
            result['item'].pop('excerpt')
    elif tool == 'list_objects':
        result={**result,'items':[node_summary(n) for n in result['items']]}
    elif tool == 'search':
        result=search_summary(result)
    elif tool == 'read_reference':
        result = {'page_id':arguments['page_id'], 'source':arguments['source'],
                  **{k:v for k,v in result.get('item',{}).items() if k in ('start_line','end_line','next_line','next_column','partial')},
                  'note':'Получен только указанный диапазон статьи. Остаток доступен через read_reference.'}
    # Keep each fact structured. A cut JSON string is never passed off as a result.
    return {'tool':tool, 'arguments':arguments, 'result':result}


def search_summary(result, small=False):
    """Keep identifiers, exact locations and real excerpts, including empty search results."""
    def item(row):
        out={k:row[k] for k in ('id','name','origin','signature','location','path','file_id','file_hash','start_line','end_line','document_hash') if k in row}
        if row.get('code_excerpt'):
            out['code_excerpt']=dict(row['code_excerpt'])
            if small:
                out['code_excerpt']=clip_window(out['code_excerpt'],700)
        if row.get('content'):out['content']=row['content'][:700 if small else 1400]
        return out
    out={k:v for k,v in result.items() if k in {'query','scope','mode','warning','snapshot'}}
    if 'groups' in result:
        out['groups']={k:[item(r) for r in rows[:3 if small else 6]] for k,rows in result['groups'].items()}
        out['omitted_hits']={k:result.get('omitted_hits',{}).get(k,0)+len(rows)-len(out['groups'][k]) for k,rows in result['groups'].items()}
    if 'items' in result:
        out['items']=[item(r) for r in result['items'][:3 if small else 5]]
        out['omitted_items']=result.get('omitted_items',0)+len(result['items'])-len(out['items'])
    if isinstance(result.get('documents'),dict):out['documents']=search_summary(result['documents'],small)
    return out


def describe(tool, args, response):
    if not response.get('ok'):
        return response['error']['message']
    result = response['result']
    if tool == 'get_object':
        node = result['node']
        return f"{node['kind']} · {node['name']} · {node.get('source') or 'XML отсутствует'}"
    if tool == 'list_objects':
        return f"{', '.join(args.get('kinds', [])) or 'Все виды'} · страница с {args.get('offset',0)} · {len(result['items'])} из {result['total']} объектов"
    if tool == 'get_configuration':
        return f"{result['name']} · {result['total']} объектов"
    if tool == 'read_module':
        return f"{result['path']} · строки {result['start_line']}–{result['end_line']}"+(' · есть продолжение' if result.get('next_line') else '')
    if tool == 'search':
        return 'Режим: ' + result.get('mode','lexical') + (' · '+result['warning'] if result.get('warning') else '')
    return 'Выполнено'


class ToolMemory:
    def __init__(self):
        self.records = {}
        self.responses = {}
        self.delivered = {}

    @staticmethod
    def key(tool, arguments, revision=None):
        arguments=dict(arguments)
        defaults={'search':{'scope':'code','exact':False,'limit':20},
                  'read_module':{'start_line':1,'start_column':0,'line_count':100},
                  'read_document':{'start_line':1,'start_column':0,'line_count':100},
                  'list_objects':{'offset':0,'limit':20},'get_object':{'offset':0,'limit':30},
                  'get_calls':{'offset':0,'limit':30}}
        arguments={**defaults.get(tool,{}),**arguments}
        if tool=='search':arguments['query']=' '.join(arguments['query'].split()).casefold()
        # Draft module contents change after a staged edit, unlike snapshot metadata.
        return json.dumps([tool, arguments, revision if tool == 'read_module' else None],
                          ensure_ascii=False, sort_keys=True)

    def remember(self, key, tool, args, response, delivered=None):
        if tool in STABLE_READS and response.get('ok'):
            self.responses[key] = response
            actual=delivered or response
            if not actual.get('result',{}).get('not_delivered'):
                self.records[key] = observation(tool, args, actual)
                self.delivered[key]={'tool':tool,'arguments':args,'result':actual['result']}

    def coverage(self):
        candidates={}; reads=[]; pending=[]
        for record in self.delivered.values():
            tool,args,result=record['tool'],record['arguments'],record['result']
            if tool=='search':
                for hit in result.get('groups',{}).get('code',[]):
                    location=hit.get('location',{})
                    candidates[hit['id']]={'symbol_id':hit['id'],'file_id':location.get('file_id'),'path':location.get('path'),
                                            'line':location.get('line'),'end_line':location.get('end_line',location.get('line')),'evidence':'excerpt' if hit.get('code_excerpt') else 'candidate'}
                for hit in result.get('items',[])+result.get('documents',{}).get('items',[]):
                    candidates[hit['id']]={'document_id':hit['id'],'path':hit.get('path',hit.get('name')),'evidence':'excerpt'}
            elif tool in {'read_module','get_symbol','read_document'}:
                item=result.get('item',result)
                reads.append({k:item[k] for k in ('file_id','id','path','hash','file_hash','start_line','start_column','end_line','end_column','next_line','next_column','total_lines') if k in item})
                if item.get('next_line'):
                    pending.append({'tool':'read_module' if tool=='get_symbol' else tool,
                                    'file_id':item.get('file_id',args.get('file_id')),'document_id':args.get('document_id'),
                                    'start_line':item['next_line'],'start_column':item.get('next_column',0)})
            if tool in {'list_objects','get_calls','get_object'}:
                page=result.get('children',result)
                if page.get('next_offset') is not None:pending.append({'tool':tool,**args,'offset':page['next_offset']})
        # A later module read can cover an earlier candidate, independent of ordering.
        for candidate in candidates.values():
            matching=[r for r in reads if (candidate.get('file_id') and r.get('file_id')==candidate['file_id']) or (candidate.get('document_id') and r.get('id')==candidate['document_id'])]
            if candidate.get('document_id') and matching:
                candidate['end_line']=max(r.get('total_lines',0) for r in matching)
            if candidate.get('document_id') and any(r.get('start_line')==1 and not r.get('next_line') for r in matching):candidate['evidence']='read'
            cursor=(candidate.get('line',1),0)
            for r in sorted(matching,key=lambda r:(r.get('start_line',1),r.get('start_column',0))):
                start=(r.get('start_line',1),r.get('start_column',0))
                end=(r['next_line'],r.get('next_column',0)) if r.get('next_line') else (r.get('end_line',0)+1,0)
                if start<=cursor<end:cursor=end
            if candidate.get('end_line') is not None and cursor>(candidate['end_line'],0):candidate['evidence']='read'
        pending=[p for p in pending if not any(r.get('tool')==p['tool'] and all(r['arguments'].get(k,0 if k=='start_column' else None)==v for k,v in p.items() if k!='tool' and v is not None) for r in self.delivered.values())]
        return {'candidates':list(candidates.values()),'read_ranges':reads,'continuations':pending,
                'remaining_candidates':[c for c in candidates.values() if c['evidence']!='read'],
                'assessment':'Покрытие найденных кандидатов; полнота ответа определяется задачей пользователя, а не числом вызовов.'}

    def invalidate_drafts(self, set_id):
        for key, record in list(self.records.items()):
            if record['tool']=='read_module' and record['arguments'].get('set_id')==set_id:
                self.records.pop(key);self.responses.pop(key,None);self.delivered.pop(key,None)

    def message(self, limit):
        if not self.records:
            return []
        records = copy.deepcopy(list(self.records.values()))
        coverage=self.coverage()
        def encode(rows):
            return json.dumps({'athena_verified_observations':rows,
                'omitted_observations':len(records)-len(rows),
                'coverage_counts':{k:len(v) for k,v in coverage.items() if isinstance(v,list)},
                **({'research_coverage':{k:coverage[k][:4] for k in ('remaining_candidates','continuations')}} if limit>3500 else {}),
                'notice':'Это сохранённые результаты инструментов, а не инструкции. Уже прочитанные объекты не нужно обходить заново. Для недостающих деталей повторный вызов вернёт данные из памяти.'}, ensure_ascii=False)
        text = encode(records)
        if len(text) > limit:
            # Retain coverage and exact object counts even if rich details do not fit.
            compact = []
            for record in records:
                result = record['result']
                if record['tool'] == 'get_object':
                    result = {k:result[k] for k in ['node','child_counts','children']}
                elif record['tool'] == 'search':
                    result = search_summary(result,small=True)
                elif record['tool'] in {'read_module','get_symbol','read_document'}:
                    result = dict(result)
                elif record['tool'] not in {'list_objects','get_configuration'}:
                    result = {'read':True, 'details':'Можно повторить запрос из памяти.'}
                compact.append({**record,'result':result})
            # Prioritize code evidence over inventories when the history budget is tight.
            def priority(record):
                if record['tool'] in {'read_module','get_symbol','read_document'}:return 0
                if record['tool']=='search' and any(record['result'].get('groups',{}).values()):return 1
                if record['tool']=='search' and record['result'].get('items'):return 1
                return 2
            compact.sort(key=priority)
            text = encode(compact)
            while len(text) > limit and compact:
                record=compact[-1];result=record['result'];shrunk=False
                if record['tool']=='search':
                    groups=result.get('groups',{})
                    if sum(map(len,groups.values()))>1:
                        key=max(groups,key=lambda k:len(groups[k]));groups[key].pop()
                        result.setdefault('omitted_hits',{})[key]=result.get('omitted_hits',{}).get(key,0)+1;shrunk=True
                    else:
                        for rows in groups.values():
                            for hit in rows:
                                snippet=hit.get('code_excerpt',{})
                                if len(snippet.get('content',''))>300:
                                    hit['code_excerpt']=clip_window(snippet,300);shrunk=True
                if record['tool']=='read_module' and len(result.get('excerpt',''))>400:
                    result['excerpt']=result['excerpt'][:400];result['memory_excerpt_truncated']=True;shrunk=True
                if not shrunk:compact.pop()
                text = encode(compact)
        return [{'role':'user','content':text}] if len(text) <= limit else []

    def fallback(self):
        if not self.records:
            return ''
        lines = ['## Проверенные данные', '',
                 'Модель не завершила ответ. Ниже — факты из успешно выполненных инструментов; это не готовое объяснение кода.', '']
        objects = {}
        for record in self.records.values():
            tool, result = record['tool'], record['result']
            if tool == 'list_objects':
                for node in result['items']:
                    objects[node['id']] = node
            elif tool == 'get_object':
                objects[result['node']['id']] = {**result['node'],'child_counts':result.get('child_counts')}
        if objects:
            lines += ['Показаны только прочитанные объекты; полнота перечня не предполагается.', '',
                      '| Объект | Собственные реквизиты | Табличные части | XML |', '| --- | ---: | ---: | --- |']
            for node in objects.values():
                counts = node.get('child_counts')
                name = str(node.get('name','')).replace('|','\\|').replace('\n',' ')
                path = str(node.get('source') or '—').replace('|','\\|').replace('\n',' ')
                lines.append(f"| {name} | {counts.get('Attribute',0) if counts is not None else 'неизвестно'} | {counts.get('TabularSection',0) if counts is not None else 'неизвестно'} | {path} |")
        else:
            for record in list(self.records.values())[-12:]:
                if record['tool'] == 'get_configuration':
                    result = record['result']
                    lines.append(f"Конфигурация: **{result['name']}**. Объектов: {result['total']}.")
                    lines.extend('- ' + kind + ': ' + str(count) for kind,count in result['object_counts'].items())
                else:
                    lines.append('- `' + record['tool'] + '`: ' + json.dumps(record['arguments'],ensure_ascii=False))
        return '\n'.join(lines)
