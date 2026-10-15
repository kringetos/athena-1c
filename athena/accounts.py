"""Local accounts, sessions and immutable AI request review records."""
from contextvars import ContextVar, copy_context
from contextlib import contextmanager
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time

ACTOR=ContextVar('athena_actor',default=None)
ROLES={'admin':'Администратор','lead':'Главный программист','programmer':'Программист'}

def privileged():
    actor=ACTOR.get()
    return actor is None or actor['role'] in {'admin','lead'}

def require_owner(owner):
    if not privileged() and owner!=(ACTOR.get() or {}).get('id'):raise PermissionError('Нет доступа к данным другого пользователя.')

def thread(target,args=()):
    context=copy_context()
    return threading.Thread(target=lambda:context.run(target,*args),daemon=True)

class Accounts:
    def __init__(self,directory):
        self.path=Path(directory)/'accounts.sqlite3';self.failures={};self.lock=threading.Lock()
        with self.db() as db:
            db.executescript('''PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,login TEXT UNIQUE COLLATE NOCASE,role TEXT NOT NULL,salt BLOB NOT NULL,password BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions(hash TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY,user_id TEXT,login TEXT,project TEXT,url TEXT,request TEXT,response TEXT,error TEXT,created REAL);
            CREATE TABLE IF NOT EXISTS corrections(id TEXT PRIMARY KEY,request_id TEXT NOT NULL REFERENCES requests(id),reviewer TEXT NOT NULL,content TEXT NOT NULL,document_id TEXT,created REAL);
            ''')
            if not db.execute('SELECT 1 FROM users LIMIT 1').fetchone():self._create(db,'admin','admin','admin')
            db.commit()

    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path,timeout=30);db.row_factory=sqlite3.Row;db.execute('PRAGMA foreign_keys=ON')
        try:yield db
        finally:db.close()

    def _create(self,db,login,password,role):
        if not isinstance(login,str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{3,64}',login):raise ValueError('Логин: 3–64 латинских символа, цифры, _, - или точка.')
        if not isinstance(password,str) or not 4<=len(password)<=256:raise ValueError('Пароль должен содержать 4–256 символов.')
        if role not in ROLES:raise ValueError('Неизвестная роль.')
        salt=secrets.token_bytes(16);identity=secrets.token_hex(16)
        db.execute('INSERT INTO users VALUES(?,?,?,?,?)',(identity,login,role,salt,hashlib.pbkdf2_hmac('sha256',password.encode(),salt,240000)))
        return identity

    def create(self,data):
        if (ACTOR.get() or {}).get('role')!='admin':raise PermissionError('Создавать аккаунты может только администратор.')
        try:
            with self.db() as db:
                identity=self._create(db,data.get('login'),data.get('password'),data.get('role'));db.commit()
        except sqlite3.IntegrityError as exc:raise ValueError('Этот логин уже занят.') from exc
        return {'id':identity}

    def users(self):
        if (ACTOR.get() or {}).get('role')!='admin':raise PermissionError('Нет доступа к управлению пользователями.')
        with self.db() as db:return [dict(r) for r in db.execute('SELECT id,login,role FROM users ORDER BY login')]

    def login(self,data,address='local'):
        login=data.get('login');password=data.get('password')
        if not isinstance(login,str) or not isinstance(password,str) or len(login)>64 or len(password)>256:raise ValueError('Некорректный логин или пароль.')
        with self.lock:
            failures=[t for t in self.failures.get(address,[]) if t>time.time()-60]
            if len(failures)>=10:raise ValueError('Слишком много попыток. Повторите через минуту.')
            failures.append(time.time());self.failures[address]=failures
        with self.db() as db:
            user=db.execute('SELECT * FROM users WHERE login=?',(login,)).fetchone()
            salt=user['salt'] if user else b'0'*16
            candidate=hashlib.pbkdf2_hmac('sha256',password.encode(),salt,240000)
            if not user or not hmac.compare_digest(candidate,user['password']):raise ValueError('Неверный логин или пароль.')
            token=secrets.token_urlsafe(32)
            db.execute('DELETE FROM sessions WHERE expires<?',(time.time(),))
            db.execute('INSERT INTO sessions VALUES(?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),user['id'],time.time()+43200));db.commit()
        with self.lock:self.failures.pop(address,None)
        return token

    def session(self,token):
        if not token or len(token)>100:return None
        with self.db() as db:
            row=db.execute('SELECT u.id,u.login,u.role FROM users u JOIN sessions s ON s.user_id=u.id WHERE s.hash=? AND s.expires>?',(hashlib.sha256(token.encode()).hexdigest(),time.time())).fetchone()
        return dict(row) if row else None

    def logout(self,token):
        with self.db() as db:db.execute('DELETE FROM sessions WHERE hash=?',(hashlib.sha256(token.encode()).hexdigest(),));db.commit()

    def record(self,url,body,response,error=''):
        actor=ACTOR.get()
        if not actor:return
        with self.db() as db:
            db.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?)',(secrets.token_hex(16),actor['id'],actor['login'],actor.get('project'),url,json.dumps(body,ensure_ascii=False),json.dumps(response,ensure_ascii=False),error,time.time()));db.commit()

    def journal(self,offset=0):
        if not privileged():raise PermissionError('Журнал доступен главному программисту и администратору.')
        offset=max(0,int(offset))
        with self.db() as db:
            rows=[dict(r) for r in db.execute('SELECT id,login,project,url,created,error FROM requests ORDER BY created DESC LIMIT 50 OFFSET ?',(offset,))]
        return {'items':rows,'next_offset':offset+50 if len(rows)==50 else None}

    def request(self,identity):
        if not privileged():raise PermissionError('Нет доступа к журналу AI.')
        with self.db() as db:
            row=db.execute('SELECT * FROM requests WHERE id=?',(identity,)).fetchone()
            if not row:raise ValueError('Запрос не найден.')
            result=dict(row);result['corrections']=[dict(r) for r in db.execute('SELECT id,reviewer,content,document_id,created FROM corrections WHERE request_id=? ORDER BY created',(identity,))]
        return result

    def correct(self,data,knowledge):
        source=self.request(data.get('id'));content=data.get('content')
        if not isinstance(content,str) or not content.strip() or len(content)>10000:raise ValueError('Введите исправление до 10000 символов.')
        if not source['project']:raise ValueError('У запроса нет проекта. Исправление нельзя привязать к базе знаний.')
        import base64
        identity=secrets.token_hex(16)
        payload=json.loads(source['request'])
        messages=payload.get('messages',payload.get('input',[])) if isinstance(payload,dict) else []
        context='\n'.join(str(m.get('content','')) for m in messages if isinstance(m,dict) and m.get('role')=='user')[:2500] if isinstance(messages,list) else str(messages)[:2500]
        text='# Проверенное исправление главного программиста\n\nКонтекст запроса:\n'+context+'\n\nПравильный ответ и указания:\n'+content.strip()
        doc=knowledge.upload(source['project'],{'name':'Исправление-'+identity[:12]+'.md','data':base64.b64encode(text.encode()).decode(),'rules':False})
        with self.db() as db:
            db.execute('INSERT INTO corrections VALUES(?,?,?,?,?,?)',(identity,source['id'],ACTOR.get()['login'],content.strip(),doc['id'],time.time()));db.commit()
        return {'id':identity,'document_id':doc['id'],'message':'Исправление сохранено. Локальные эмбеддинги рассчитываются автоматически.'}
