"""Local versioned knowledge store. SQLite is canonical; vectors are derived data."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from athena.catalog import read_text, canonical, text_hash
from athena.bsl_index import extract, search_text, VERSION

def digest(value):return hashlib.sha256(value.encode('utf-8')).hexdigest()
def encoded(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))

SCHEMA='''
CREATE TABLE projects(id TEXT PRIMARY KEY, name TEXT NOT NULL, active_snapshot TEXT);
CREATE TABLE snapshots(id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), root TEXT NOT NULL, created REAL NOT NULL, parser_version TEXT NOT NULL);
CREATE TABLE metadata(snapshot_id TEXT NOT NULL REFERENCES snapshots(id), node_id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(snapshot_id,node_id));
CREATE TABLE blobs(hash TEXT PRIMARY KEY, content TEXT NOT NULL);
CREATE TABLE analyses(id TEXT PRIMARY KEY, blob_hash TEXT NOT NULL REFERENCES blobs(hash), parser_version TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE modules(snapshot_id TEXT NOT NULL REFERENCES snapshots(id), file_id TEXT NOT NULL, path TEXT NOT NULL, owner_id TEXT NOT NULL, owner_name TEXT NOT NULL, module_name TEXT NOT NULL, blob_hash TEXT NOT NULL REFERENCES blobs(hash), analysis_id TEXT NOT NULL REFERENCES analyses(id), PRIMARY KEY(snapshot_id,file_id));
CREATE TABLE symbols(id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL REFERENCES snapshots(id), file_id TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL, start_line INTEGER NOT NULL, declaration_line INTEGER NOT NULL, end_line INTEGER NOT NULL, data TEXT NOT NULL, FOREIGN KEY(snapshot_id,file_id) REFERENCES modules(snapshot_id,file_id));
CREATE INDEX symbols_scope ON symbols(snapshot_id,file_id);
CREATE TABLE chunks(id TEXT PRIMARY KEY, symbol_id TEXT REFERENCES symbols(id), snapshot_id TEXT NOT NULL REFERENCES snapshots(id), file_id TEXT NOT NULL, blob_hash TEXT NOT NULL REFERENCES blobs(hash), start_line INTEGER NOT NULL, end_line INTEGER NOT NULL, content TEXT NOT NULL, content_hash TEXT NOT NULL);
CREATE TABLE embedding_models(id TEXT PRIMARY KEY, provider TEXT NOT NULL, model TEXT NOT NULL, dimensions INTEGER NOT NULL, version TEXT NOT NULL);
CREATE TABLE embeddings(chunk_id TEXT NOT NULL REFERENCES chunks(id), model_id TEXT NOT NULL REFERENCES embedding_models(id), content_hash TEXT NOT NULL, vector BLOB NOT NULL, PRIMARY KEY(chunk_id,model_id));
CREATE TABLE calls(snapshot_id TEXT NOT NULL REFERENCES snapshots(id), source_symbol TEXT NOT NULL REFERENCES symbols(id), name TEXT NOT NULL, receiver TEXT, line INTEGER NOT NULL, target_symbol TEXT REFERENCES symbols(id), resolution TEXT NOT NULL);
CREATE INDEX calls_target ON calls(snapshot_id,target_symbol);
CREATE VIRTUAL TABLE symbol_search USING fts5(symbol_id UNINDEXED,snapshot_id UNINDEXED,name,context,body,tokenize='unicode61');
'''

VECTOR_SCHEMA='''
CREATE TABLE embedding_cache(model_id TEXT NOT NULL REFERENCES embedding_models(id), input_hash TEXT NOT NULL, vector BLOB NOT NULL, PRIMARY KEY(model_id,input_hash));
CREATE TABLE semantic_indexes(snapshot_id TEXT NOT NULL REFERENCES snapshots(id), model_id TEXT NOT NULL REFERENCES embedding_models(id), chunk_count INTEGER NOT NULL, created REAL NOT NULL, PRIMARY KEY(snapshot_id,model_id));
'''

class Knowledge:
    def __init__(self,path,read_only=False):
        self.path=Path(path);self.read_only=read_only
        self._statuses={};self._objects={}
        if read_only:return
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            version=db.execute('PRAGMA user_version').fetchone()[0]
            if version>2:raise ValueError('Версия базы знаний новее версии Athena.')
            if version==0:db.executescript('BEGIN IMMEDIATE;'+SCHEMA+'PRAGMA user_version=1;COMMIT;')
            if version<2:db.executescript('BEGIN IMMEDIATE;'+VECTOR_SCHEMA+'PRAGMA user_version=2;COMMIT;')

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=30) if self.read_only else sqlite3.connect(self.path,timeout=30)
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:yield db
        finally:db.close()

    def index(self,catalog,project_key,progress=lambda *args:None):
        project_id=digest(project_key)
        nodes=catalog.nodes
        modules=[]
        owners={f['id']:node for node in nodes.values() if node['kind']=='Module' for f in node['files']}
        for fid,path in catalog.files.items():
            if path.suffix.lower() not in {'.bsl','.os'}:continue
            text=canonical(read_text(path));h=text_hash(text);module=owners[fid];owner=nodes[module['parent']]
            modules.append((fid,path.relative_to(catalog.root).as_posix(),owner,module,text,h))
        fingerprint=encoded({'parser':VERSION,'metadata':list(nodes.values()),'files':[(m[0],m[5]) for m in sorted(modules,key=lambda m:m[0])]})
        snapshot=digest(project_id+fingerprint)
        # Durable analysis cache is independent of snapshot publication. A
        # failed/restarted import reuses completed modules; readers continue
        # seeing the previous complete snapshot while parsing runs.
        with self.connect() as cache:
            exists=cache.execute('SELECT 1 FROM snapshots WHERE id=?',(snapshot,)).fetchone()
            if not exists:
                for index,(fid,path,owner,module,text,h) in enumerate(modules,1):
                    progress('Разбор BSL · '+path,index-1,len(modules))
                    analysis_id=digest(h+VERSION)
                    if not cache.execute('SELECT 1 FROM analyses WHERE id=?',(analysis_id,)).fetchone():
                        analysis=extract(text)
                        cache.execute('INSERT OR IGNORE INTO blobs VALUES(?,?)',(h,text))
                        cache.execute('INSERT OR IGNORE INTO analyses VALUES(?,?,?,?)',(analysis_id,h,VERSION,encoded(analysis)))
                        cache.commit()
                    progress('Разбор BSL · '+path,index,len(modules))
        with self.connect() as db:
            # One serialized writer, short-lived readers. Publication is one transaction.
            db.execute('BEGIN IMMEDIATE')
            try:
                db.execute('INSERT INTO projects(id,name) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name',(project_id,nodes[catalog.configuration]['name']))
                if not db.execute('SELECT 1 FROM snapshots WHERE id=?',(snapshot,)).fetchone():
                    db.execute('INSERT INTO snapshots VALUES(?,?,?,?,?)',(snapshot,project_id,str(catalog.root),time.time(),VERSION))
                    db.executemany('INSERT INTO metadata VALUES(?,?,?)',[(snapshot,n['id'],encoded(n)) for n in nodes.values()])
                    all_symbols=[]
                    for index,(fid,path,owner,module,text,h) in enumerate(modules,1):
                        progress('Индексирование кода SQLite · '+path,index-1,len(modules))
                        db.execute('INSERT OR IGNORE INTO blobs VALUES(?,?)',(h,text))
                        analysis_id=digest(h+VERSION)
                        cached=db.execute('SELECT data FROM analyses WHERE id=?',(analysis_id,)).fetchone()
                        analysis=json.loads(cached[0])
                        context=' / '.join([owner['kind'],owner['name'],module['name'],path,owner['properties'].get('Synonym','')])
                        db.execute('INSERT INTO modules VALUES(?,?,?,?,?,?,?,?)',(snapshot,fid,path,owner['id'],owner['name'],module['name'],h,analysis_id))
                        covered=set()
                        for ordinal,sym in enumerate(analysis['symbols']):
                            sid=digest(snapshot+fid+str(ordinal));sym={**sym,'id':sid,'owner_id':owner['id'],'file_id':fid,'path':path,'owner_name':owner['name'],'module_name':module['name'],'file_hash':h}
                            db.execute('INSERT INTO symbols VALUES(?,?,?,?,?,?,?,?,?)',(sid,snapshot,fid,sym['name'],sym['kind'],sym['start_line'],sym['declaration_line'],sym['end_line'],encoded(sym)))
                            db.execute('INSERT INTO symbol_search VALUES(?,?,?,?,?)',(sid,snapshot,search_text(sym['name']),search_text(context+' '+sym['comment']),search_text(sym['code'])))
                            all_symbols.append((sym,owner['kind']))
                            covered.update(range(sym['start_line'],sym['end_line']+1))
                            # Bounded windows preserve their function and original line positions.
                            lines=sym['code'].splitlines()
                            for start in range(0,len(lines),100):
                                content='\n'.join(lines[start:start+120]);cid=digest(sid+str(start))
                                db.execute('INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?,?)',(cid,sid,snapshot,fid,h,sym['start_line']+start,min(sym['end_line'],sym['start_line']+start+119),content,text_hash(content)))
                        # Preserve module-level declarations and unparsed sections for future RAG.
                        lines=text.splitlines()
                        for start in range(0,len(lines),100):
                            end=min(len(lines),start+100)
                            if all(line in covered for line in range(start+1,end+1)):continue
                            content='\n'.join(lines[start:end]);cid=digest(snapshot+fid+'module'+str(start))
                            db.execute('INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?,?)',(cid,None,snapshot,fid,h,start+1,end,content,text_hash(content)))
                        progress('Индексирование кода SQLite · '+path,index,len(modules))
                    progress('Связывание вызовов SQLite',0,len(all_symbols))
                    local={};common={}
                    for sym,kind in all_symbols:
                        local.setdefault((sym['file_id'],sym['name'].casefold()),[]).append(sym['id'])
                        if kind=='CommonModule' and sym['export']:common.setdefault((sym['owner_name'].casefold(),sym['name'].casefold()),[]).append(sym['id'])
                    for symbol_number,(sym,kind) in enumerate(all_symbols,1):
                        for call in sym['calls']:
                            candidates=common.get((call['receiver'].casefold(),call['name'].casefold()),[]) if call['receiver'] else local.get((sym['file_id'],call['name'].casefold()),[])
                            shadowed=call['receiver'] and call['receiver'].casefold() in {name.casefold() for name in sym.get('local_names',[])}
                            target=candidates[0] if len(candidates)==1 and sym.get('syntax_valid',True) and not shadowed else None
                            # Qualified receivers may be shadowed; mark matches as candidates.
                            resolution='candidate' if target and call['receiver'] else 'local' if target else 'unresolved'
                            db.execute('INSERT INTO calls VALUES(?,?,?,?,?,?,?)',(snapshot,sym['id'],call['name'],call['receiver'],call['line'],target,resolution))
                        if symbol_number%128==0 or symbol_number==len(all_symbols):
                            progress('Связывание вызовов SQLite',symbol_number,len(all_symbols))
                db.execute('UPDATE projects SET active_snapshot=? WHERE id=?',(snapshot,project_id))
                progress('Сохранение индекса SQLite',0,0)
                db.commit()
            except BaseException:db.rollback();raise
        return snapshot

    def status(self,snapshot):
        if not snapshot:return {'ready':False}
        if snapshot in self._statuses:return dict(self._statuses[snapshot])
        with self.connect() as db:
            result={'ready':True,'snapshot':snapshot,'parser':VERSION,'semantic_search':False}
            for table in ['metadata','modules','symbols','chunks','calls']:
                result[table]=db.execute(f'SELECT count(*) FROM {table} WHERE snapshot_id=?',(snapshot,)).fetchone()[0]
            result['warnings']=db.execute("SELECT coalesce(sum(json_array_length(a.data,'$.warnings')),0) FROM modules m JOIN analyses a ON a.id=m.analysis_id WHERE m.snapshot_id=?",(snapshot,)).fetchone()[0]
            result['diagnostics']=[]
            for row in db.execute("SELECT m.path,json_extract(a.data,'$.warnings') AS warnings FROM modules m JOIN analyses a ON a.id=m.analysis_id WHERE m.snapshot_id=? AND json_array_length(a.data,'$.warnings')>0",(snapshot,)):
                result['diagnostics'].extend({**w,'path':row['path']} for w in json.loads(row['warnings']))
                if len(result['diagnostics'])>=100:break
            result['diagnostics']=result['diagnostics'][:100]
        self._statuses={snapshot:result}
        return dict(result)

    def metadata_inventory(self, snapshot, kinds, limit=100, offset=0):
        """List top-level metadata, including declarations without BSL modules."""
        with self.connect() as db:
            root = db.execute("SELECT node_id FROM metadata WHERE snapshot_id=? "
                              "AND json_extract(data,'$.kind')='Configuration' LIMIT 1",
                              (snapshot,)).fetchone()
            if not root:
                raise ValueError('Метаданные текущего снимка недоступны. Обновите проект.')
            where = "snapshot_id=? AND json_extract(data,'$.parent')=?"
            args = [snapshot, root['node_id']]
            if kinds:
                where += " AND json_extract(data,'$.kind') IN (" + ','.join('?' for _ in kinds) + ')'
                args.extend(kinds)
            else:
                where += " AND json_extract(data,'$.kind') NOT IN ('Module','File')"
            total = db.execute('SELECT count(*) FROM metadata WHERE ' + where, args).fetchone()[0]
            rows = db.execute("SELECT data FROM metadata WHERE " + where +
                              " ORDER BY json_extract(data,'$.kind'),json_extract(data,'$.name') "
                              "COLLATE NOCASE LIMIT ? OFFSET ?", [*args, limit, offset]).fetchall()
            counts = {}
            for row in db.execute("SELECT json_extract(data,'$.parent') AS parent, "
                                  "json_extract(data,'$.kind') AS kind, count(*) AS total "
                                  "FROM metadata WHERE snapshot_id=? GROUP BY parent, kind", (snapshot,)):
                counts.setdefault(row['parent'], {})[row['kind']] = row['total']
        objects = [json.loads(row['data']) for row in rows]
        for node in objects:
            node['child_counts'] = None if node.get('missing') else counts.get(node['id'], {})
        return {'objects': objects, 'total': total, 'complete': offset == 0 and len(objects) == total,
                'requested_kinds': kinds, 'next_offset': offset+len(objects) if offset+len(objects)<total else None}

    @staticmethod
    def query_terms(value):
        import re
        stop=set('найди найти покажи где как каким какая какие какой почему зачем что это в во на из по с со к для и или а у мне пожалуйста задается установить задаётся'.split())
        expanded=re.sub(r'(?<=[a-zа-яё0-9])(?=[A-ZА-ЯЁ])',' ',value).casefold().replace('ё','е')
        terms=[]
        for word in re.findall(r'\w+',expanded):
            if word in stop or len(word)<2:continue
            if len(word)>=5 and re.fullmatch('[а-я]+',word):
                for ending in ['ями','ами','ого','ему','ому','ов','ам','ах','ую','ая','ой','ые','ий','ый','ое','ее','ю','е','а','ы','и','у','я']:
                    if word.endswith(ending) and len(word)-len(ending)>=4:
                        word=word[:-len(ending)];break
            if word not in terms:terms.append(word)
        return terms[:20]

    def search_context(self,snapshot,query,limit=30):
        import re
        if not isinstance(query,str) or len(query)>500:raise ValueError('Запрос должен быть строкой до 500 символов.')
        # Preserve literal identifier semantics; never turn an unknown identifier
        # into unrelated matches on its individual CamelCase components.
        if re.fullmatch(r'\w+(?:\.\w+)*',query.strip()) and re.search(r'[A-ZА-ЯЁ_]',query):
            return self.search(snapshot,query,limit)
        terms=self.query_terms(query)
        if not terms:return []
        with self.connect() as db:
            if snapshot not in self._objects:
                rows=db.execute("SELECT json_extract(data,'$.name','$.properties.Synonym','$.source') AS info FROM metadata WHERE snapshot_id=? AND json_extract(data,'$.kind') IN ('Document','Catalog','InformationRegister','AccumulationRegister','CommonModule','DataProcessor','Report')",(snapshot,))
                self._objects={snapshot:[json.loads(row['info']) for row in rows]}
            scopes=[]
            for name,synonym,source in self._objects[snapshot]:
                words=self.query_terms(name+' '+(synonym or ''))
                # Common 1C business wording: UNF calls a customer invoice
                # "СчетНаОплату", while the supplier variant has its own name.
                if source and source.startswith('Documents/') and name=='СчетНаОплату':
                    words+=self.query_terms('Счёт покупателю')
                matched=[term for term in terms if any(word.startswith(term) or term.startswith(word) for word in words)]
                if len(matched)>=2 and source and source.endswith('.xml'):
                    scopes.append((len(matched),source[:-4]+'/',matched))
            scopes.sort(key=lambda scope:-scope[0]);scopes=scopes[:8]
            topic=[term for term in terms if not scopes or term not in scopes[0][2]] or terms
            expression=' OR '.join('"'+term+'"*' for term in topic)
            where='';parameters=[expression,snapshot]
            if scopes:
                where=' AND ('+' OR '.join("m.path LIKE ? ESCAPE '!'" for _ in scopes)+')'
                parameters.extend(prefix.replace('!','!!').replace('%','!%').replace('_','!_')+'%' for _,prefix,_ in scopes)
            parameters.append(min(max(limit,1),50))
            sql='SELECT s.data,bm25(symbol_search,0,0,8,3,1) AS score FROM symbol_search JOIN symbols s ON s.id=symbol_search.symbol_id JOIN modules m ON m.snapshot_id=s.snapshot_id AND m.file_id=s.file_id WHERE symbol_search MATCH ? AND s.snapshot_id=?'+where+' ORDER BY score LIMIT ?'
            rows=db.execute(sql,parameters).fetchall()
            # An explicit object scope is respected even if no matching code is found.
            results=[]
            for row in rows:
                sym=json.loads(row['data']);sym.pop('calls',None);sym.pop('local_names',None)
                code=sym.pop('code');lines=code.splitlines()
                scores=[sum(term in line.casefold().replace('ё','е') for term in topic) for line in lines]
                if scores and max(scores)>0:
                    line=scores.index(max(scores));sym['chunk_start_line']=max(sym['start_line'],sym['start_line']+line-3)
                sym['match']='object-code' if scopes else 'keywords'
                results.append(sym)
            return results

    def search(self,snapshot,query,limit=30):
        if not isinstance(query,str) or len(query)>500:raise ValueError('Запрос должен быть строкой до 500 символов.')
        import re
        words=list(dict.fromkeys(re.findall(r'[\w]+',query.casefold(),flags=re.UNICODE)))[:20]
        if not words or not snapshot:return []
        primary=' AND '.join('"'+word+'"*' for word in words)
        expanded=re.sub(r'(?<=[a-zа-яё0-9])(?=[A-ZА-ЯЁ])',' ',query).casefold().replace('_',' ')
        parts=list(dict.fromkeys(re.findall(r'[\w]+',expanded,flags=re.UNICODE)))[:20]
        secondary=' AND '.join('"'+word+'"*' for word in parts)
        expression=f'({primary}) OR ({secondary})' if secondary and secondary!=primary else primary
        with self.connect() as db:
            rows=None
            # A tiny project must not rank matches across every large configuration in the DB.
            count=db.execute('SELECT count(*) FROM symbols WHERE snapshot_id=?',(snapshot,)).fetchone()[0]
            if count<=2000:
                db.execute('PRAGMA temp_store=MEMORY')
                db.execute("CREATE VIRTUAL TABLE temp.scoped_search USING fts5(symbol_id UNINDEXED,name,context,body,tokenize='unicode61')")
                size=0
                for row in db.execute("SELECT s.data,json_extract(o.data,'$.kind') owner_kind,json_extract(o.data,'$.properties.Synonym') synonym FROM symbols s JOIN modules m ON m.snapshot_id=s.snapshot_id AND m.file_id=s.file_id LEFT JOIN metadata o ON o.snapshot_id=m.snapshot_id AND o.node_id=m.owner_id WHERE s.snapshot_id=?",(snapshot,)):
                    size+=len(row['data'])
                    if size>8_000_000:break
                    sym=json.loads(row['data']);context=' / '.join([row['owner_kind'] or '',sym['owner_name'],sym['module_name'],sym['path'],row['synonym'] or ''])
                    db.execute('INSERT INTO scoped_search VALUES(?,?,?,?)',(sym['id'],search_text(sym['name']),search_text(context+' '+sym['comment']),search_text(sym['code'])))
                else:
                    rows=db.execute('''SELECT s.data,bm25(scoped_search,0,8,3,1) AS score FROM scoped_search JOIN symbols s ON s.id=scoped_search.symbol_id WHERE scoped_search MATCH ? ORDER BY score LIMIT ?''',(expression,min(max(limit,1),50))).fetchall()
            if rows is None:
                rows=db.execute('''SELECT s.data,bm25(symbol_search,0,0,8,3,1) AS score FROM symbol_search JOIN symbols s ON s.id=symbol_search.symbol_id WHERE symbol_search MATCH ? AND s.snapshot_id=? ORDER BY score LIMIT ?''',(expression,snapshot,min(max(limit,1),50))).fetchall()
            result=[json.loads(row['data']) for row in rows]
            result.sort(key=lambda r:(r['name'].casefold()!=query.strip().casefold(),query.strip().casefold() not in r['name'].casefold()))
            return [{k:r[k] for k in ['id','name','kind','parameters','parameter_details','async','syntax_valid','export','start_line','declaration_line','end_line','owner_id','file_id','path','owner_name','module_name','file_hash','comment']} for r in result]

    def symbol(self,snapshot,symbol_id):
        with self.connect() as db:
            row=db.execute('SELECT data FROM symbols WHERE snapshot_id=? AND id=?',(snapshot,symbol_id)).fetchone()
            if not row:raise ValueError('Функция отсутствует в текущей версии проекта.')
            result=json.loads(row[0])
            result['outgoing']=[dict(r) for r in db.execute('SELECT name,receiver,line,target_symbol,resolution FROM calls WHERE snapshot_id=? AND source_symbol=?',(snapshot,symbol_id))]
            result['incoming']=[dict(r) for r in db.execute('SELECT c.source_symbol,c.line,c.resolution,s.name FROM calls c JOIN symbols s ON s.id=c.source_symbol WHERE c.snapshot_id=? AND c.target_symbol=?',(snapshot,symbol_id))]
            return result

    def semantic_status(self, snapshot, provider):
        if not snapshot:return {'ready':False,'indexed':0,'total':0,'model':provider.name}
        with self.connect() as db:
            total=db.execute('SELECT count(*) FROM chunks WHERE snapshot_id=? AND symbol_id IS NOT NULL',(snapshot,)).fetchone()[0]
            marker=db.execute('SELECT chunk_count FROM semantic_indexes WHERE snapshot_id=? AND model_id=?',(snapshot,provider.model_id)).fetchone()
            indexed=db.execute('SELECT count(*) FROM embeddings e JOIN chunks c ON c.id=e.chunk_id WHERE c.snapshot_id=? AND e.model_id=?',(snapshot,provider.model_id)).fetchone()[0]
            return {'ready':bool(marker and marker[0]==total and indexed==total),'indexed':indexed,'total':total,'model':provider.name}

    @staticmethod
    def vector_bytes(vector, dimensions):
        import numpy as np
        value=np.asarray(vector,dtype='<f4')
        if value.shape!=(dimensions,) or not np.isfinite(value).all():
            raise ValueError('Модель вернула некорректный вектор или размерность.')
        norm=float(np.linalg.norm(value.astype(np.float64)))
        if not np.isfinite(norm) or norm<1e-12:raise ValueError('Модель вернула нулевой или некорректный вектор.')
        return (value/norm).astype('<f4').tobytes()

    def index_embeddings(self, snapshot, provider, progress=lambda *args:None):
        status=self.semantic_status(snapshot,provider)
        if status['ready']:return
        total=status['total'];done=status['indexed']
        import numpy as np
        import re
        readable=lambda value:re.sub(r'(?<=[a-zа-яё0-9])(?=[A-ZА-ЯЁ])',' ',value).replace('_',' ')
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO embedding_models VALUES(?,?,?,?,?)',(provider.model_id,provider.provider,provider.model,provider.dimensions,provider.version))
            model=db.execute('SELECT dimensions,version FROM embedding_models WHERE id=?',(provider.model_id,)).fetchone()
            if model['dimensions']!=provider.dimensions or model['version']!=provider.version:raise ValueError('Версия модели не совпала с сохранённой.')
            db.commit()
        last=''
        progress('Расчёт локальных эмбеддингов',done,total)
        while True:
            # Keyset pagination and narrow JSON projection keep memory bounded.
            # No reader/writer transaction stays open during model computation.
            with self.connect() as db:
                rows=db.execute("""SELECT c.id,c.content,json_extract(s.data,'$.comment','$.name','$.owner_name') AS info
                    FROM chunks c JOIN symbols s ON s.id=c.symbol_id
                    WHERE c.snapshot_id=? AND c.id>? AND NOT EXISTS
                    (SELECT 1 FROM embeddings e WHERE e.chunk_id=c.id AND e.model_id=?)
                    ORDER BY c.id LIMIT ?""",(snapshot,last,provider.model_id,getattr(provider,'config',{}).get('batch_size',16))).fetchall()
                if not rows:break
                documents=[];cache={}
                for row in rows:
                    comment,name,owner=json.loads(row['info'])
                    text=f"{comment}\nМетод: {readable(name)}.\nОбъект: {readable(owner)}.\nИсходный код:\n{row['content']}"
                    key=digest(text);documents.append((row['id'],key,text))
                    cached=db.execute('SELECT vector FROM embedding_cache WHERE model_id=? AND input_hash=?',(provider.model_id,key)).fetchone()
                    if cached:
                        self.vector_bytes(np.frombuffer(cached[0],dtype='<f4'),provider.dimensions)
                        cache[key]=cached[0]
            progress('Расчёт локальных эмбеддингов',done,total)
            unique={key:text for _,key,text in documents if key not in cache}
            if unique:
                vectors=list(provider.documents(list(unique.values())))
                if len(vectors)!=len(unique):raise ValueError('Количество эмбеддингов не совпало с количеством фрагментов.')
                for key,vector in zip(unique,vectors):cache[key]=self.vector_bytes(vector,provider.dimensions)
            progress('Расчёт локальных эмбеддингов',done,total)
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                try:
                    db.executemany('INSERT OR IGNORE INTO embedding_cache VALUES(?,?,?)',[(provider.model_id,key,value) for key,value in cache.items()])
                    db.executemany('INSERT OR REPLACE INTO embeddings VALUES(?,?,?,?)',[(cid,provider.model_id,key,cache[key]) for cid,key,_ in documents])
                    db.commit()
                except BaseException:db.rollback();raise
            done+=len(documents);last=rows[-1]['id']
            progress('Расчёт локальных эмбеддингов',done,total)
        progress('Сохранение смыслового индекса',done,total)
        with self.connect() as db:
            # Only a complete snapshot receives the publication marker. Saved
            # batches remain resumable but unavailable for semantic retrieval.
            count=db.execute('SELECT count(*) FROM embeddings e JOIN chunks c ON c.id=e.chunk_id WHERE c.snapshot_id=? AND e.model_id=?',(snapshot,provider.model_id)).fetchone()[0]
            if count!=total:raise ValueError('Смысловой индекс неполон. Продолжите индексацию.')
            db.execute('INSERT OR REPLACE INTO semantic_indexes VALUES(?,?,?,?)',(snapshot,provider.model_id,total,time.time()))
            db.commit()

    def semantic_search(self,snapshot,query,provider,limit=30,min_similarity=.78):
        if not isinstance(query,str) or len(query)>500:raise ValueError('Запрос должен быть строкой до 500 символов.')
        if not query.strip():return []
        if not self.semantic_status(snapshot,provider)['ready']:raise ValueError('Смысловой индекс текущего проекта ещё не готов.')
        import numpy as np
        vector=np.frombuffer(self.vector_bytes(provider.query(query),provider.dimensions),dtype='<f4')
        scores={};best_chunks={}
        with self.connect() as db:
            cursor=db.execute('SELECT c.symbol_id,c.start_line,c.end_line,e.vector FROM embeddings e JOIN chunks c ON c.id=e.chunk_id WHERE c.snapshot_id=? AND e.model_id=?',(snapshot,provider.model_id))
            while rows:=cursor.fetchmany(512):
                values=[np.frombuffer(r['vector'],dtype='<f4') for r in rows]
                if any(v.shape!=(provider.dimensions,) or not np.isfinite(v).all() for v in values):raise ValueError('Повреждён векторный индекс.')
                similarities=np.stack(values)@vector
                for row,score in zip(rows,similarities):
                    if float(score)>=min_similarity and float(score)>scores.get(row['symbol_id'],-1):
                        scores[row['symbol_id']]=float(score);best_chunks[row['symbol_id']]=row
            ids=sorted(scores,key=scores.get,reverse=True)[:min(max(limit,1),50)]
            result=[]
            for sid in ids:
                sym=json.loads(db.execute('SELECT data FROM symbols WHERE id=? AND snapshot_id=?',(sid,snapshot)).fetchone()[0])
                sym.pop('code',None);sym.pop('calls',None);sym.pop('local_names',None)
                result.append({**sym,'similarity':scores[sid],'match':'semantic','chunk_start_line':best_chunks[sid]['start_line'],'chunk_end_line':best_chunks[sid]['end_line']})
            return result

    def hybrid_search(self,snapshot,query,provider,limit=30):
        lexical=self.search_context(snapshot,query,50)
        # Identifier-shaped requests need literal matches rather than nearest unrelated names.
        import re
        if not lexical and re.fullmatch(r'\w+(?:\.\w+)*',query.strip()) and re.search(r'[A-ZА-ЯЁ_]',query):return []
        semantic=self.semantic_search(snapshot,query,provider,50)
        combined={};scores={}
        for weight,rows,kind in [(2,lexical,'lexical'),(1,semantic,'semantic')]:
            for rank,row in enumerate(rows,1):
                sid=row['id'];scores[sid]=scores.get(sid,0)+weight/(60+rank)
                combined[sid]={**combined.get(sid,{}),**row,'match':'hybrid' if sid in combined else kind}
        ordered=sorted(combined,key=lambda sid:(combined[sid]['name'].casefold()!=query.strip().casefold(),-scores[sid]))
        return [{**combined[sid],'rank_score':scores[sid]} for sid in ordered[:min(max(limit,1),50)]]
