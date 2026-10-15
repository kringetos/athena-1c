"""Managed Git sources, scoped retrieval and reviewed, explicit publication."""
from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import sqlite3
import subprocess
import threading
import time
from urllib.parse import urlsplit

from athena.document_knowledge import chunks, extract

TEXT = {'.md', '.txt', '.rst', '.bsl', '.os', '.xml', '.json', '.yaml', '.yml', '.py', '.js', '.ts', '.html', '.css', '.sql', '.pdf', '.docx'}
EXCLUDED = {'.git', '.athena', '.venv', 'node_modules', '__pycache__', '.env', '.ssh', 'credentials', 'secrets'}


def remote_url(value):
    if not isinstance(value, str) or not value.strip() or value.startswith('-') or any(c in value for c in '\r\n\x00'):
        raise ValueError('Укажите адрес Git-репозитория.')
    value = value.strip()
    if '://' in value:
        parsed = urlsplit(value)
        if parsed.scheme not in {'https', 'ssh'} or not parsed.hostname or parsed.password or parsed.query or parsed.fragment or (parsed.scheme == 'https' and parsed.username):
            raise ValueError('Используйте HTTPS или SSH без пароля и токена в адресе. Авторизация — через Git Credential Manager или SSH.')
    elif not re.fullmatch(r'[\w.-]+@[\w.-]+:[\w./~-]+', value):
        if not Path(value).is_absolute() or not Path(value).is_dir():
            raise ValueError('Укажите HTTPS/SSH URL или абсолютный путь к локальному репозиторию.')
    return value


class GitProjects:
    def __init__(self, directory):
        self.directory = Path(directory) / 'git'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'sources.sqlite3'
        self.hooks = self.directory / 'empty-hooks'
        self.hooks.mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.stop = threading.Event()
        with self.db() as db:
            db.executescript('''PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS repos(id TEXT PRIMARY KEY, project TEXT NOT NULL, name TEXT NOT NULL,
                    url TEXT NOT NULL, path TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                    automatic INTEGER NOT NULL DEFAULT 0, checked REAL, error TEXT NOT NULL DEFAULT '', warning TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS files(repo TEXT NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
                    path TEXT NOT NULL, hash TEXT NOT NULL, content TEXT NOT NULL, PRIMARY KEY(repo,path));''')
        self.worker = threading.Thread(target=self.watch, daemon=True, name='athena-git')
        self.worker.start()

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def git(self, path, *args, env=None):
        environment = dict(os.environ, GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='Never',
                           GIT_SSH_COMMAND='ssh -oBatchMode=yes', GIT_CONFIG_NOSYSTEM='1')
        if env: environment.update(env)
        command = ['git', '--literal-pathspecs', '-c', 'core.quotepath=false', '-c', 'core.hooksPath='+str(self.hooks),
                   '-c', 'protocol.allow=never', '-c', 'protocol.https.allow=always',
                   '-c', 'protocol.ssh.allow=always', '-c', 'protocol.file.allow=always',
                   '-c', 'submodule.recurse=false', '-c', 'core.fsmonitor=false']
        if path: command += ['-C', str(path)]
        try:
            result = subprocess.run(command + list(args), capture_output=True, env=environment, timeout=60,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except FileNotFoundError as exc:
            raise ValueError('Установите Git и перезапустите Athena.') from exc
        except subprocess.TimeoutExpired as exc:
            raise ValueError('Git не ответил за 60 секунд. Проверьте сеть и авторизацию.') from exc
        if result.returncode:
            message = result.stderr.decode('utf-8', errors='replace')
            message = re.sub(r'https?://[^\s/@]+(?::[^\s/@]*)?@', 'https://***@', message)
            raise ValueError('Git: '+message[-1800:].strip())
        return result.stdout.decode('utf-8', errors='replace').strip('\n')

    def repo(self, project, identity):
        with self.db() as db:
            row = db.execute('SELECT * FROM repos WHERE project=? AND id=?', (project, identity)).fetchone()
        if not row: raise ValueError('Git-источник текущего проекта не найден.')
        return dict(row)

    def connect(self, project, data):
        with self.lock:
            url = remote_url(data.get('url'))
            name = data.get('name') or url.rstrip('/\\').split('/')[-1].removesuffix('.git')
            if not isinstance(name, str) or len(name)>120: raise ValueError('Название — до 120 символов.')
            with self.db() as db:
                if db.execute('SELECT count(*) FROM repos WHERE project=?', (project,)).fetchone()[0]>=20:
                    raise ValueError('Можно подключить до 20 Git-источников к проекту.')
                if db.execute('SELECT 1 FROM repos WHERE project=? AND url=?', (project,url)).fetchone():
                    raise ValueError('Репозиторий уже подключён.')
            identity = secrets.token_hex(16)
            path = self.directory / identity
            self.git(None, 'clone', '--', url, str(path))
            with self.db() as db:
                db.execute('INSERT INTO repos(id,project,name,url,path) VALUES(?,?,?,?,?)', (identity,project,name,url,str(path)))
            repo = self.repo(project,identity)
            self.index(repo)
            return self.status(repo)

    def safe_file(self, repo, name):
        if not isinstance(name,str) or not name or '\\' in name or ':' in name or '\x00' in name:
            raise ValueError('Недопустимый путь файла.')
        parts = PurePosixPath(name).parts
        if any(p.endswith(('.', ' ')) or any(ord(c)<32 for c in p) or p.split('.')[0].upper() in {'CON','PRN','AUX','NUL',*(f'COM{i}' for i in range(1,10)),*(f'LPT{i}' for i in range(1,10))} for p in parts):
            raise ValueError('Недопустимое имя файла.')
        if PurePosixPath(name).is_absolute() or any(p in {'.','..'} or p.lower() in EXCLUDED or p.lower().startswith(('.env.', 'credentials.', 'secrets.')) for p in parts):
            raise ValueError('Файл находится за пределами исходников.')
        path = Path(repo['path'])
        root = path.resolve()
        for part in parts:
            path = path / part
            if path.is_symlink() or getattr(path,'is_junction',lambda:False)(): raise ValueError('Ссылки не поддерживаются.')
        if not path.resolve().is_relative_to(root): raise ValueError('Файл находится вне репозитория.')
        return path

    def paths(self, repo):
        raw = self.git(repo['path'], 'ls-files', '-z', '--cached', '--others', '--exclude-standard')
        return sorted(set(p for p in raw.split('\x00') if p))

    def index(self, repo):
        rows=[]; total=0; skipped=0
        for name in self.paths(repo):
            try:
                path=self.safe_file(repo,name)
                if path.suffix.lower() not in TEXT or not path.is_file(): continue
                if path.stat().st_size>2_000_000: skipped+=1; continue
                data=path.read_bytes()
                if total+len(data)>20_000_000 or len(rows)>=2000: skipped+=1; continue
                if path.suffix.lower() in {'.pdf','.docx','.md','.txt'}: text=extract(path.name,data)
                else:
                    if b'\x00' in data: continue
                    try: text=data.decode('utf-8-sig')
                    except UnicodeDecodeError: text=data.decode('cp1251')
                if len(text)>250000: skipped+=1; continue
                rows.append((repo['id'],name,sha256(data).hexdigest(),text));total+=len(data)
            except (ValueError,OSError,UnicodeError): skipped+=1
        with self.db() as db:
            db.execute('DELETE FROM files WHERE repo=?',(repo['id'],))
            db.executemany('INSERT INTO files VALUES(?,?,?,?)',rows)
            db.execute('UPDATE repos SET checked=?,error=?,warning=? WHERE id=?',
                       (time.time(),'',f'Пропущено файлов: {skipped}. Лимит: 2 МБ/файл, 20 МБ и 2000 файлов на источник.' if skipped else '',repo['id']))

    def status(self, repo):
        path=repo['path']
        branch=self.git(path,'branch','--show-current')
        try: head=self.git(path,'rev-parse','--verify','HEAD')
        except ValueError:
            if self.git(path,'for-each-ref','--format=%(objectname)',f'refs/heads/{branch}'): raise
            head=''
        raw=self.git(path,'status','--porcelain=v1','-z','--untracked-files=all','--no-renames')
        changes=[{'status':line[:2],'path':line[3:]} for line in raw.split('\x00') if line]
        upstream=self.git(path,'for-each-ref','--format=%(upstream:short)',f'refs/heads/{branch}').strip()
        upstream_ref=self.git(path,'for-each-ref','--format=%(upstream)',f'refs/heads/{branch}').strip()
        if upstream_ref and not self.git(path,'for-each-ref','--format=%(objectname)',upstream_ref): upstream=''
        ahead=behind=0
        if upstream:
            ahead,behind=map(int,self.git(path,'rev-list','--left-right','--count','HEAD...'+upstream).split())
        with self.db() as db:
            count=db.execute('SELECT count(*) FROM files WHERE repo=?',(repo['id'],)).fetchone()[0]
            current=dict(db.execute('SELECT * FROM repos WHERE id=?',(repo['id'],)).fetchone())
        return {**current,'branch':branch,'head':head,'upstream':upstream,'ahead':ahead,'behind':behind,'changes':changes,'files':count}

    def listing(self, project):
        with self.lock,self.db() as db:
            rows=db.execute('SELECT * FROM repos WHERE project=? ORDER BY name,id',(project,)).fetchall()
            items=[]
            for row in rows:
                try: items.append(self.status(dict(row)))
                except (ValueError,OSError) as exc: items.append({**dict(row),'error':str(exc),'changes':[]})
            return {'repositories':items}

    def sync(self, repo):
        path=repo['path']
        # Check configured origin every time: credentials must never be persisted in Athena.
        remote_url(self.git(path,'remote','get-url','origin'))
        self.git(path,'fetch','--prune','origin')
        state=self.status(repo)
        if state['changes']: raise ValueError('Есть локальные изменения. Сначала сохраните коммит; входящие изменения получены, рабочие файлы не обновлены.')
        if not state['branch']: raise ValueError('Выберите ветку: репозиторий находится в detached HEAD.')
        if not state['upstream']: raise ValueError('У ветки нет upstream. Сначала отправьте её в origin.')
        self.git(path,'merge','--ff-only',state['upstream'])
        self.index(repo)
        return self.status(repo)

    def review(self, repo, paths):
        if not isinstance(paths,list) or not paths or len(paths)>100 or any(not isinstance(p,str) for p in paths):
            raise ValueError('Выберите от 1 до 100 файлов.')
        paths=sorted(set(paths)); state=self.status(repo)
        changed={c['path'] for c in state['changes']}
        if not set(paths)<=changed: raise ValueError('Список изменений устарел. Обновите его.')
        diff=[]; hashes=[]
        for name in paths:
            path=self.safe_file(repo,name)
            if path.exists() and (not path.is_file() or path.stat().st_size>2_000_000): raise ValueError('Для просмотра выберите файлы до 2 МБ.')
            hashes.append((name,sha256(path.read_bytes()).hexdigest() if path.exists() else None))
            if next(c['status'] for c in state['changes'] if c['path']==name)=='??':
                content=path.read_bytes()
                text=content.decode('utf-8',errors='replace') if b'\x00' not in content else '[Двоичный файл]'
                diff.append('+++ '+name+' (новый файл)\n'+text)
            else: diff.append(self.git(repo['path'],'diff','HEAD','--',name))
        token=sha256(json.dumps([state['head'],state['changes'],hashes,diff],ensure_ascii=False).encode()).hexdigest()
        full='\n'.join(diff)
        if len(full)>200000: raise ValueError('Слишком большой diff. Выберите меньше файлов.')
        return {'token':token,'diff':full,'paths':paths,'head':state['head']}

    def import_project(self, project, data, catalog):
        with self.lock:
            repo=self.repo(project,data.get('id'))
            if self.status(repo)['changes']: raise ValueError('Перед копированием сохраните локальные изменения в Git-коммит.')
            prefix=data.get('prefix','')
            if not isinstance(prefix,str): raise ValueError('Укажите каталог назначения.')
            planned=[];total=0
            for source in catalog.files.values():
                relative=source.relative_to(catalog.root).as_posix()
                # Use the same traversal guards for source and destination.
                self.safe_file({'path':str(catalog.root)},relative)
                target=self.safe_file(repo,(prefix.rstrip('/')+'/' if prefix else '')+relative)
                if source.suffix.lower() not in TEXT: continue
                if source.stat().st_size>2_000_000: raise ValueError('В проекте есть файл больше 2 МБ: '+relative)
                binary=source.read_bytes();total+=len(binary)
                if total>20_000_000 or len(planned)>=2000: raise ValueError('Копирование ограничено 20 МБ и 2000 файлами. Используйте внешний Git для крупных выгрузок.')
                planned.append((target,binary))
            for target,binary in planned:
                target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(binary)
            self.index(repo)
            return self.status(repo)

    def action(self, project, data):
        with self.lock:
            repo=self.repo(project,data.get('id')); action=data.get('action')
            if action=='sync': return self.sync(repo)
            if action=='branch':
                name=data.get('branch')
                if not isinstance(name,str) or not name or name.startswith('-'): raise ValueError('Укажите имя ветки.')
                self.git(repo['path'],'check-ref-format','--branch',name)
                if self.status(repo)['changes']: raise ValueError('Сначала сохраните локальные изменения в коммит.')
                self.git(repo['path'],'checkout','-b',name)
                return self.status(repo)
            if action=='refresh': self.index(repo); return self.status(repo)
            if action=='configure':
                if type(data.get('enabled')) is not bool or type(data.get('automatic')) is not bool: raise ValueError('Укажите enabled и automatic.')
                with self.db() as db: db.execute('UPDATE repos SET enabled=?,automatic=? WHERE id=?',(data['enabled'],data['automatic'],repo['id']))
                return self.status(repo)
            if action=='disconnect':
                with self.db() as db: db.execute('DELETE FROM repos WHERE id=?',(repo['id'],))
                return {'disconnected':True,'path':repo['path']}
            if action=='review': return self.review(repo,data.get('paths'))
            if action=='read':
                path=self.safe_file(repo,data.get('path'))
                if path.suffix.lower() not in TEXT- {'.pdf','.docx'} or path.stat().st_size>250000: raise ValueError('Выберите текстовый файл до 250 КБ.')
                raw=path.read_bytes()
                return {'content':raw.decode('utf-8-sig'),'hash':sha256(raw).hexdigest()}
            if action=='write':
                path=self.safe_file(repo,data.get('path')); content=data.get('content')
                if path.suffix.lower() not in TEXT-{'.pdf','.docx'} or not isinstance(content,str) or len(content.encode())>250000: raise ValueError('Укажите текст до 250 КБ.')
                current=sha256(path.read_bytes()).hexdigest() if path.exists() else None
                if current!=data.get('expected_hash'): raise ValueError('Файл изменился. Перечитайте его перед сохранением.')
                path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(content.encode('utf-8'))
                self.index(repo);return self.status(repo)
            if action=='commit':
                review=self.review(repo,data.get('paths'))
                if review['token']!=data.get('token'): raise ValueError('Изменения обновились после просмотра diff. Повторите просмотр.')
                message=data.get('message')
                if not isinstance(message,str) or not message.strip() or len(message)>2000: raise ValueError('Укажите сообщение коммита до 2000 символов.')
                if self.git(repo['path'],'diff','--cached','--name-only'): raise ValueError('В индексе уже есть изменения. Завершите их коммит во внешнем Git.')
                self.git(repo['path'],'add','--',*review['paths'])
                self.git(repo['path'],'commit','-m',message,'--only','--',*review['paths'])
                self.index(repo);return self.status(repo)
            if action=='push':
                state=self.status(repo)
                if not state['branch'] or state['head']!=data.get('head') or state['branch']!=data.get('branch'): raise ValueError('Ветка или коммит изменились. Обновите состояние перед push.')
                remote_url(self.git(repo['path'],'remote','get-url','origin'))
                self.git(repo['path'],'push','--set-upstream','origin','HEAD:refs/heads/'+state['branch'])
                return self.status(repo)
            raise ValueError('Неизвестное действие Git.')

    def search(self, project, query, limit=5):
        terms=set(re.findall(r'\w{2,}',query.lower())); scored=[]
        with self.db() as db:
            rows=db.execute('SELECT f.*,r.name FROM files f JOIN repos r ON r.id=f.repo WHERE r.project=? AND r.enabled=1',(project,)).fetchall()
        for row in rows:
            if not any(t in (row['path']+' '+row['content']).lower() for t in terms): continue
            for start,end,part in chunks(row['content']):
                score=sum(t in (row['path']+' '+part).lower() for t in terms)
                if score: scored.append((score,{'id':row['repo']+':'+row['path'],'repository_id':row['repo'],'name':row['name']+'/'+row['path'],
                    'path':row['path'],'document_hash':row['hash'],'start_line':start,'end_line':end,'content':part,'source':'git'}))
        scored.sort(key=lambda p:(-p[0],p[1]['name'],p[1]['start_line']))
        return {'items':[item for _,item in scored[:max(1,min(limit,10))]],'mode':'lexical','warning':''}

    def read_snapshot(self, project, identity):
        with self.db() as db:
            row=db.execute("SELECT r.id||':'||f.path id,r.name||'/'||f.path name,f.hash,f.content,r.enabled FROM files f JOIN repos r ON r.id=f.repo WHERE r.project=? AND r.id||':'||f.path=?",(project,identity)).fetchone()
        if not row:raise ValueError('Git-источник текущего проекта не найден.')
        return dict(row)

    def revision(self, project):
        with self.db() as db:
            rows=[tuple(r) for r in db.execute('SELECT r.id,r.enabled,f.path,f.hash FROM repos r LEFT JOIN files f ON f.repo=r.id WHERE r.project=? ORDER BY r.id,f.path',(project,))]
        return sha256(json.dumps(rows).encode()).hexdigest()

    def watch(self):
        while not self.stop.wait(60):
            self.check_once()

    def check_once(self):
        with self.db() as db: repos=[dict(r) for r in db.execute('SELECT * FROM repos')]
        for repo in repos:
            if self.stop.is_set(): return
            with self.lock:
                try:
                    repo=self.repo(repo['project'],repo['id'])
                    self.index(repo)
                    if repo['automatic']: self.sync(repo)
                except (ValueError,OSError) as exc:
                    with self.db() as db: db.execute('UPDATE repos SET error=?,checked=? WHERE id=?',(str(exc),time.time(),repo['id']))

    def close(self):
        self.stop.set()
        self.worker.join(timeout=65)
