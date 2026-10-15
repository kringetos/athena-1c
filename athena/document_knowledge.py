"""Project documents, explicit project rules and resumable local embeddings."""
import base64
from contextlib import contextmanager
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from zipfile import ZipFile, BadZipFile
from xml.etree import ElementTree as ET

MAX_FILE = 2_000_000
MAX_TEXT = 250_000
MAX_RULES = 12_000


def extract(name, data):
    suffix=Path(name).suffix.lower()
    if suffix in {'.txt','.md'}:
        try:text=data.decode('utf-16' if data.startswith((b'\xff\xfe',b'\xfe\xff')) else 'utf-8-sig')
        except UnicodeDecodeError:
            try:text=data.decode('cp1251')
            except UnicodeDecodeError as exc:raise ValueError('Сохраните текст в UTF-8.') from exc
    elif suffix=='.pdf':
        from pypdf import PdfReader
        try:
            reader=PdfReader(BytesIO(data))
            if reader.is_encrypted:raise ValueError('PDF защищён паролем. Загрузите открытую копию.')
            if len(reader.pages)>200:raise ValueError('PDF должен содержать не более 200 страниц.')
            pages=[]
            for number,page in enumerate(reader.pages,1):
                pages.append(page.extract_text() or '')
                if sum(map(len,pages))>MAX_TEXT:raise ValueError('Слишком много текста в документе.')
            if not any(p.strip() for p in pages):
                raise ValueError('В PDF нет текстового слоя. Сначала выполните OCR.')
            text='\n'.join(f'[Страница {i}]\n{p}' for i,p in enumerate(pages,1))
        except ValueError:raise
        except Exception as exc:raise ValueError('Не удалось прочитать PDF. Проверьте файл.') from exc
    elif suffix=='.docx':
        try:
            with ZipFile(BytesIO(data)) as archive:
                info=archive.getinfo('word/document.xml')
                if info.file_size>8_000_000:raise ValueError('Слишком большой текст DOCX.')
                xml=archive.read(info)
                if b'<!DOCTYPE' in xml or b'<!ENTITY' in xml:raise ValueError('Недопустимый XML в DOCX.')
                root=ET.fromstring(xml)
                ns='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
                text='\n'.join(''.join((n.text or '') if n.tag==ns+'t' else '\t' if n.tag==ns+'tab' else '\n' if n.tag in {ns+'br',ns+'cr'} else '' for n in p.iter()) for p in root.iter(ns+'p'))
        except (BadZipFile,KeyError,ET.ParseError,RuntimeError) as exc:raise ValueError('Не удалось прочитать DOCX.') from exc
    else:raise ValueError('Поддерживаются MD, TXT, PDF с текстовым слоем и DOCX.')
    text=text.replace('\r\n','\n').replace('\r','\n').replace('\x00','').strip()
    if not text:raise ValueError('В документе нет текста.')
    if len(text)>MAX_TEXT:raise ValueError('В документе должно быть не более 250000 символов.')
    return text


def chunks(text):
    start=0
    while start<len(text):
        end=min(len(text),start+1400)
        if end<len(text):
            boundary=text.rfind('\n',start+700,end)
            if boundary>start:end=boundary
        part=text[start:end]
        yield text[:start].count('\n')+1,text[:end].count('\n')+1,part
        if end==len(text):break
        start=end-120


class DocumentKnowledge:
    def __init__(self, workspace, directory):
        self.w=workspace;self.path=Path(directory)/'document_knowledge.sqlite3'
        self.lock=threading.RLock();self.runtime_lock=threading.RLock()
        self.stop=threading.Event();self.worker=None;self.runtime=None;self.runtime_config=None
        with self.connect() as db:
            db.executescript('''PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY, project TEXT NOT NULL, name TEXT NOT NULL, hash TEXT NOT NULL, content TEXT NOT NULL, rules INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS chunks(document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE, ordinal INTEGER NOT NULL, start_line INTEGER NOT NULL, end_line INTEGER NOT NULL, content TEXT NOT NULL, hash TEXT NOT NULL, PRIMARY KEY(document_id,ordinal));
            CREATE TABLE IF NOT EXISTS vectors(hash TEXT NOT NULL, model TEXT NOT NULL, vector BLOB NOT NULL, PRIMARY KEY(hash,model));
            CREATE INDEX IF NOT EXISTS documents_project ON documents(project);
            ''')
            db.execute("UPDATE documents SET status='pending',error='' WHERE status='indexing'");db.commit()
        self.schedule()

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=30);db.row_factory=sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:yield db
        finally:db.close()

    def revision(self,project):
        with self.connect() as db:
            rows=[tuple(r) for r in db.execute('SELECT id,hash,rules,enabled FROM documents WHERE project=? ORDER BY id',(project,))]
        return sha256(json.dumps(rows).encode()).hexdigest()

    def listing(self,project):
        model=self.w.embedder.model_id
        with self.connect() as db:
            rows=db.execute('''SELECT d.id,d.name,d.hash,d.rules,d.enabled,d.status,d.error,d.created,length(d.content) characters,
                count(c.ordinal) total,count(v.vector) done FROM documents d LEFT JOIN chunks c ON c.document_id=d.id
                LEFT JOIN vectors v ON v.hash=c.hash AND v.model=? WHERE d.project=? GROUP BY d.id ORDER BY d.created DESC''',(model,project)).fetchall()
        items=[dict(r) for r in rows]
        for item in items:
            if item['done']==item['total']:item['status']='ready';item['error']=''
            elif item['status']=='ready':item['status']='pending'
        return {'documents':items,'model':self.w.embedder.name,'revision':self.revision(project)}

    def upload(self,project,data):
        name=data.get('name');raw=data.get('data');rules=data.get('rules',False)
        if not isinstance(name,str) or not name.strip() or len(name)>180 or '/' in name or '\\' in name:raise ValueError('Укажите имя файла без пути, до 180 символов.')
        if type(rules) is not bool:raise ValueError('Некорректный признак правил проекта.')
        if not isinstance(raw,str) or len(raw)>2_700_000:raise ValueError('Размер файла — до 2 МБ.')
        try:binary=base64.b64decode(raw,validate=True)
        except ValueError as exc:raise ValueError('Некорректное содержимое файла.') from exc
        if not binary or len(binary)>MAX_FILE:raise ValueError('Размер файла — до 2 МБ.')
        text=extract(name,binary);digest=sha256(text.encode()).hexdigest()
        with self.lock,self.connect() as db:
            if db.execute('SELECT 1 FROM documents WHERE project=? AND hash=?',(project,digest)).fetchone():raise ValueError('Этот текст уже загружен в текущий проект.')
            count=db.execute('SELECT count(*) FROM documents WHERE project=?',(project,)).fetchone()[0]
            if count>=100:raise ValueError('В проекте допускается до 100 документов.')
            used=db.execute('SELECT coalesce(sum(length(content)),0) FROM documents WHERE project=? AND rules=1 AND enabled=1',(project,)).fetchone()[0]
            if rules and used+len(text)>MAX_RULES:raise ValueError('Обязательные правила вместе должны занимать не более 12000 символов. Остальное загрузите как справочный документ.')
            identity=secrets.token_hex(16)
            db.execute('INSERT INTO documents(id,project,name,hash,content,rules,status,created) VALUES(?,?,?,?,?,?,?,?)',(identity,project,name.strip(),digest,text,int(rules),'pending',time.time()))
            db.executemany('INSERT INTO chunks VALUES(?,?,?,?,?,?)',[(identity,i,a,b,t,sha256(t.encode()).hexdigest()) for i,(a,b,t) in enumerate(chunks(text))]);db.commit()
        self.schedule();return {'id':identity}

    def configure(self,project,data):
        identity=data.get('id');enabled=data.get('enabled');rules=data.get('rules')
        if type(enabled) is not bool or type(rules) is not bool:raise ValueError('Укажите enabled и rules.')
        with self.lock,self.connect() as db:
            doc=db.execute('SELECT * FROM documents WHERE id=? AND project=?',(identity,project)).fetchone()
            if not doc:raise ValueError('Документ текущего проекта не найден.')
            used=db.execute('SELECT coalesce(sum(length(content)),0) FROM documents WHERE project=? AND rules=1 AND enabled=1 AND id<>?',(project,identity)).fetchone()[0]
            if enabled and rules and used+len(doc['content'])>MAX_RULES:raise ValueError('Обязательные правила вместе должны занимать не более 12000 символов.')
            db.execute("UPDATE documents SET enabled=?,rules=?,status='pending',error='' WHERE id=?",(enabled,rules,identity));db.commit()
        self.schedule();return self.listing(project)

    def rules(self,project):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT id,name,hash,content FROM documents WHERE project=? AND enabled=1 AND rules=1 ORDER BY created,id',(project,))]

    def read(self,project,identity):
        with self.connect() as db:
            row=db.execute('SELECT id,name,hash,content,rules,enabled FROM documents WHERE project=? AND id=?',(project,identity)).fetchone()
        if not row:raise ValueError('Документ текущего проекта не найден.')
        return dict(row)

    def runtime_for_settings(self):
        config=dict(self.w.embedding_settings)
        if config!=self.runtime_config or self.runtime is not None and type(self.runtime) is not type(self.w.embedder):
            if self.runtime:
                close=getattr(self.runtime,'close',None)
                if close:close()
            self.runtime=self.w.make_embedder(config);self.runtime_config=config
        return self.runtime

    def schedule(self):
        with self.lock:
            if self.stop.is_set() or self.worker and self.worker.is_alive():return
            with self.connect() as db:
                pending=db.execute("SELECT 1 FROM documents d JOIN chunks c ON c.document_id=d.id LEFT JOIN vectors v ON v.hash=c.hash AND v.model=? WHERE d.enabled=1 AND d.status<>'error' AND v.hash IS NULL LIMIT 1",(self.w.embedder.model_id,)).fetchone()
            if not pending:return
            self.worker=threading.Thread(target=self.run,daemon=True,name='athena-document-embeddings');self.worker.start()

    def run(self):
        try:
            while not self.stop.is_set():
                model=self.w.embedder.model_id
                with self.lock,self.connect() as db:
                    row=db.execute("SELECT d.id,c.hash,c.content FROM documents d JOIN chunks c ON c.document_id=d.id LEFT JOIN vectors v ON v.hash=c.hash AND v.model=? WHERE d.enabled=1 AND d.status<>'error' AND v.hash IS NULL ORDER BY d.created,c.ordinal LIMIT 1",(model,)).fetchone()
                    if not row:
                        db.execute("UPDATE documents SET status='ready',error='' WHERE enabled=1 AND status<>'error'");db.commit()
                        self.worker=None;return
                    db.execute("UPDATE documents SET status='indexing' WHERE id=?",(row['id'],));db.commit()
                try:
                    import numpy as np
                    with self.runtime_lock:
                        runtime=self.runtime_for_settings();vector=runtime.documents([row['content']])[0]
                        vector=np.asarray(vector,dtype=np.float32)
                        if vector.ndim!=1 or vector.size!=runtime.dimensions or not np.isfinite(vector).all():raise ValueError('Модель вернула некорректный вектор.')
                        actual_model=runtime.model_id
                    with self.connect() as db:
                        db.execute('INSERT OR REPLACE INTO vectors VALUES(?,?,?)',(row['hash'],actual_model,vector.tobytes()));db.commit()
                except Exception as exc:
                    with self.connect() as db:
                        db.execute("UPDATE documents SET status='error',error=? WHERE id=?",(str(exc)[:1000],row['id']));db.commit()
        finally:
            with self.lock:
                if self.worker is threading.current_thread():self.worker=None

    def search(self,project,query,limit=5):
        import numpy as np
        limit=max(1,min(limit,10));model=self.w.embedder.model_id
        with self.connect() as db:
            rows=db.execute('''SELECT d.id,d.name,d.hash document_hash,c.ordinal,c.start_line,c.end_line,c.content,v.vector
                FROM documents d JOIN chunks c ON c.document_id=d.id LEFT JOIN vectors v ON v.hash=c.hash AND v.model=?
                WHERE d.project=? AND d.enabled=1''',(model,project)).fetchall()
        if not rows:return {'items':[],'mode':'lexical','warning':''}
        terms=set(re.findall(r'\w{2,}',query.lower()));query_vector=None;warning=''
        if any(r['vector'] for r in rows):
            try:
                with self.runtime_lock:
                    runtime=self.runtime_for_settings()
                    if runtime.model_id==model:query_vector=np.asarray(runtime.query(query),dtype=np.float32)
            except Exception:warning='Смысловой поиск документов недоступен; используется поиск по словам.'
        else:warning='Документы ещё индексируются; используется поиск по словам.'
        scored=[]
        for row in rows:
            words=(row['name']+' '+row['content']).lower();lexical=sum(t in words for t in terms)
            similarity=0
            if query_vector is not None and row['vector']:
                value=np.frombuffer(row['vector'],dtype=np.float32)
                if value.shape==query_vector.shape:similarity=float(np.dot(value,query_vector)/max(float(np.linalg.norm(value)*np.linalg.norm(query_vector)),1e-12))
            if lexical or similarity>.35:
                scored.append((lexical/(len(terms) or 1)+similarity,dict(row)))
        scored.sort(key=lambda item:item[0],reverse=True)
        return {'items':[{k:v for k,v in r.items() if k!='vector'} for _,r in scored[:limit]],'mode':'hybrid' if query_vector is not None else 'lexical','warning':warning}

    def close(self):
        self.stop.set()
        if self.runtime:
            cancel=getattr(self.runtime,'cancel',None)
            if cancel is not None:cancel.set()
        if self.worker:self.worker.join(timeout=3)
        if self.runtime:
            close=getattr(self.runtime,'close',None)
            if close:close()
