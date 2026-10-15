"""Local HTTP adapter for Athena."""
from __future__ import annotations

import argparse
import json
import secrets
import os
import socket
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from athena.catalog import Catalog
from athena.editing import prepare, text_hash, upload_module
from athena.objects import prepare_object, upload_object, verify_object
from athena.catalog import read_text
from athena.knowledge import Knowledge
from athena.embeddings import LocalEmbeddings, settings as embedding_settings
from athena.gpu_runtime import GPUInstaller, ManagedGPU, runtime_ready
from athena.ai import AI, validate as validate_ai
from athena.exporter import Profiles, registered_bases, platforms, validate_platform, normalize, export_configuration, configuration_changed, ExportCancelled

BASE = Path(__file__).resolve().parent.parent
WEB = BASE / 'web'
STATE = BASE / '.athena' / 'recent.json'


class LocalServer(ThreadingHTTPServer):
    # Windows must not allow two app processes to share the same listening port.
    allow_reuse_address = False

    def server_bind(self):
        if os.name == 'nt':
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class Workspace:
    def user_profile(self):
        try:
            profile=json.loads((STATE.parent / 'user.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            profile={}
        return profile if isinstance(profile,dict) else {}

    def onboarding_status(self):
        profile=self.user_profile()
        import onnxruntime as ort
        return {'completed':profile.get('completed') is True,'name':profile.get('name',''),'platform':profile.get('platform',''),'platforms':self.available_platforms(),
                'settings':dict(self.embedding_settings),'ai':self.ai.status(),
                'hardware':{'cpu_threads':os.cpu_count() or 1,'cuda_available':runtime_ready(STATE.parent) or 'CUDAExecutionProvider' in ort.get_available_providers()},
                'gpu_install':self.gpu_installer.status(),
                'presets':{mode:embedding_settings({'mode':mode}) for mode in ['comfort','fast']}}

    def complete_onboarding(self, data):
        name=data.get('name','')
        if not isinstance(name,str) or len(name)>80 or any(ord(c)<32 for c in name):
            raise ValueError('Имя должно быть строкой до 80 символов.')
        config=embedding_settings(data.get('settings'))
        platform=data.get('platform','')
        if platform:platform=validate_platform(platform)
        elif not isinstance(platform,str):raise ValueError('Некорректный путь к платформе 1С.')
        ai=data.get('ai')
        if ai is not None:
            if not isinstance(ai,dict):raise ValueError('Некорректные настройки AI.')
            validate_ai(ai.get('provider'),ai)
            key=ai.get('api_key','')
            if not isinstance(key,str) or len(key)>4096 or any(c in key for c in '\r\n\x00'):
                raise ValueError('Некорректный API-ключ.')
            if not isinstance(ai.get('clear_key',False),bool):raise ValueError('Некорректный параметр clear_key.')
        with self.lock:
            if self.semantic_worker and self.semantic_worker.is_alive():
                raise ValueError('Приостановите индексацию перед первоначальной настройкой.')
        self.set_semantic({'enabled':False,'settings':config})
        if ai is not None:self.ai.configure(ai)
        path=STATE.parent / 'user.json'
        temporary=path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'completed':True,'name':name.strip(),'platform':platform},ensure_ascii=False),encoding='utf-8')
        temporary.replace(path)
        return {'completed':True,'name':name.strip()}

    def __init__(self):
        self.gpu_installer=GPUInstaller(STATE.parent,self.refresh_gpu_provider)
        self.catalog = None
        self.lock = threading.Lock()
        self.job = {'status': 'idle'}
        self.token = secrets.token_urlsafe(32)
        self.profiles = Profiles()
        self.passwords = {}
        self.cancel_export = threading.Event()
        self.active_connection = None
        self.revision = 0
        self.auto = {'enabled': False, 'interval': 60, 'phase': 'Выключено', 'error': '', 'last_check': None}
        self.auto_stop = threading.Event()
        self.auto_wake = threading.Event()
        self.auto_thread = None
        self.drafts = {}
        self.knowledge = Knowledge(STATE.parent / 'knowledge.sqlite3')
        self.knowledge_snapshot = None
        self.ai = AI(STATE.parent / 'ai.json')
        from athena.reference import Reference
        from athena.language import LanguageKnowledge
        from athena.completion import Completion
        self.language = LanguageKnowledge(Reference(STATE.parent / 'reference.sqlite3'), self.knowledge.path)
        self.completion = Completion(STATE.parent / 'completion.json', self.language, self.ai)
        from athena.change_sets import ChangeSets
        self.change_sets = ChangeSets(STATE.parent)
        from athena.agent_tools import AgentTools
        self.agent_tools = AgentTools(self, STATE.parent)
        from athena.code_agent import CodeAgent
        self.code_agent = CodeAgent(self, STATE.parent)
        from athena.documentation import Documentation
        self.documentation = Documentation(self, STATE.parent)
        self.semantic_config = STATE.parent / 'semantic.json'
        try:
            saved = json.loads(self.semantic_config.read_text(encoding='utf-8'))
            self.semantic_enabled = saved.get('enabled') is True
            self.embedding_settings = embedding_settings(saved.get('settings'))
        except (OSError, ValueError, AttributeError):
            self.semantic_enabled = False
            self.embedding_settings = embedding_settings()
        self.embedder = self.make_embedder(self.embedding_settings)
        self.semantic_job = {'status':'idle'}
        self.semantic_worker = None

    def make_embedder(self, config):
        if config['device']=='gpu' and runtime_ready(STATE.parent):return ManagedGPU(STATE.parent,config)
        return LocalEmbeddings(STATE.parent / 'models',config)

    def refresh_gpu_provider(self):
        with self.lock:
            if self.semantic_worker and self.semantic_worker.is_alive():return
            if self.embedding_settings['device']=='gpu' and not isinstance(self.embedder,ManagedGPU):
                self.embedder=self.make_embedder(self.embedding_settings)

    def knowledge_status(self):
        with self.lock:
            snapshot=self.knowledge_snapshot
            enabled=self.semantic_enabled
            job=dict(self.semantic_job)
        result=self.knowledge.status(snapshot)
        semantic=self.knowledge.semantic_status(snapshot,self.embedder)
        result.update(semantic_search=enabled and semantic['ready'],semantic={**semantic,'enabled':enabled,'job':job,'settings':dict(self.embedding_settings)})
        return result

    def set_semantic(self, data):
        enabled=data.get('enabled')
        if not isinstance(enabled,bool):raise ValueError('Укажите enabled: true или false.')
        config = embedding_settings(data['settings']) if 'settings' in data else self.embedding_settings
        self.semantic_config.parent.mkdir(parents=True,exist_ok=True)
        with self.lock:
            if config != self.embedding_settings and self.semantic_worker and self.semantic_worker.is_alive():
                raise ValueError('Приостановите индексацию и дождитесь остановки перед изменением настроек.')
            temporary=self.semantic_config.with_suffix('.tmp')
            temporary.write_text(json.dumps({'enabled':enabled,'settings':config}),encoding='utf-8')
            temporary.replace(self.semantic_config)
            if config != self.embedding_settings or (config['device']=='gpu' and runtime_ready(STATE.parent) and not isinstance(self.embedder,ManagedGPU)):
                close=getattr(self.embedder,'close',None)
                if close:close()
                self.embedder = self.make_embedder(config)
                self.embedding_settings = config
            self.semantic_enabled=enabled
            cancel = getattr(self.embedder, 'cancel', None)
            if cancel is not None:
                if enabled: cancel.clear()
                else: cancel.set()
        if enabled:self.schedule_embeddings()
        return self.knowledge_status()

    def schedule_embeddings(self):
        with self.lock:
            if not self.semantic_enabled or not self.knowledge_snapshot:return
            if self.semantic_worker and self.semantic_worker.is_alive():return
            self.semantic_job={'status':'running','phase':'Подготовка локальной модели E5','done':0,'total':0}
            def run():
                def progress(phase,done,total):
                    with self.lock:
                        if not self.semantic_enabled:raise ValueError('Смысловой поиск выключен.')
                        self.semantic_job.update(phase=phase,done=done,total=total)
                try:
                    while True:
                        with self.lock:
                            snapshot=self.knowledge_snapshot
                            if not self.semantic_enabled:
                                self.semantic_job={'status':'disabled'}
                                self.semantic_worker=None
                                return
                        self.knowledge.index_embeddings(snapshot,self.embedder,progress)
                        with self.lock:
                            if snapshot!=self.knowledge_snapshot:continue
                            self.semantic_job={'status':'ready','phase':'Смысловой индекс готов'}
                            self.semantic_worker=None
                            return
                except Exception as exc:
                    with self.lock:
                        self.semantic_job={'status':'error','error':str(exc)} if self.semantic_enabled else {**self.semantic_job,'status':'paused','phase':'Смысловая индексация приостановлена'}
                        self.semantic_worker=None
            self.semantic_worker=threading.Thread(target=run,daemon=True)
            self.semantic_worker.start()

    def search_functions(self, query, mode='hybrid'):
        if mode not in {'lexical','semantic','hybrid'}:raise ValueError('Неизвестный режим поиска.')
        with self.lock:
            snapshot=self.knowledge_snapshot
            enabled=self.semantic_enabled
        if not snapshot:raise ValueError('Сначала откройте проект и дождитесь индексации.')
        response={'snapshot':snapshot,'mode':mode,'warning':''}
        ready=enabled and self.knowledge.semantic_status(snapshot,self.embedder)['ready']
        if mode=='semantic' and not ready:raise ValueError('Включите смысловой поиск и дождитесь индексации текущего проекта.')
        if mode=='lexical' or not ready:
            response.update(mode='lexical',results=self.knowledge.search_context(snapshot,query))
            if mode=='hybrid':response['warning']='Выполнен поиск по объектам и ключевым словам; смысловой индекс пока недоступен.'
        else:
            try:
                response['results']=(self.knowledge.semantic_search(snapshot,query,self.embedder) if mode=='semantic' else self.knowledge.hybrid_search(snapshot,query,self.embedder))
            except (ValueError,OSError,RuntimeError) as exc:
                if mode=='semantic':raise ValueError(str(exc)) from exc
                response.update(mode='lexical',results=self.knowledge.search_context(snapshot,query),warning='Смысловой поиск недоступен: '+str(exc))
        return response

    def ai_context(self, question):
        from athena.ai_metadata import inventory_request, structure_request
        structure = structure_request(question)
        kinds = structure['kinds'] if structure else inventory_request(question)
        if kinds is None:
            return self.search_functions(question)
        with self.lock:
            snapshot = self.knowledge_snapshot
        if not snapshot:
            raise ValueError('Сначала откройте проект и дождитесь индексации.')
        inventory = self.knowledge.metadata_inventory(snapshot, kinds)
        if structure:
            inventory.update({k:v for k,v in structure.items() if k in {'field','fields'}})
        return {'snapshot': snapshot, 'mode': 'metadata', 'results': [], 'inventory': inventory}

    def set_context(self):
        with self.lock:
            if not self.catalog or self.job['status'] == 'running': raise ValueError('Откройте проект и дождитесь текущей операции.')
            profile = next((p for p in self.profiles.all() if p['id'] == self.active_connection), None)
            return self.active_connection or str(self.catalog.root), self.catalog, profile

    def change_set_action(self, data):
        project, catalog, profile = self.set_context(); action = data.get('action'); identity = data.get('id'); revision = data.get('revision')
        if action == 'create': return self.change_sets.create(project, data.get('title'))
        if action == 'add':
            if not isinstance(data.get('entry'), dict): raise ValueError('Укажите изменение.')
            return self.change_sets.add(identity, project, revision, catalog, data['entry'])
        if action == 'enable': return self.change_sets.enable(identity, project, revision, data.get('entry_id'), data.get('enabled'))
        if action == 'archive': return self.change_sets.archive(identity, project, revision, data.get('restore', False))
        value = self.change_sets.read(identity)
        if value['project'] != project: raise ValueError('Набор относится к другому проекту.')
        if action == 'read': return value
        if action == 'review': return {'checks': self.change_sets.review(value)}
        if action == 'validate': return self.change_sets.validate(value, catalog)
        if action == 'prepare':
            result = self.change_sets.prepare(identity, project, revision, catalog, profile)
            if result.get('prepared'):
                with self.lock: self.drafts[result['id']] = result['set']['package']
            return result
        raise ValueError('Неизвестное действие с набором.')

    def prepare_object(self, data):
        with self.lock:
            if self.job['status']=='running' or not self.catalog:
                raise ValueError('Откройте проект и дождитесь завершения текущей операции.')
            profile=next((p for p in self.profiles.all() if p['id']==self.active_connection),None)
            draft,preview=prepare_object(self.catalog,data,profile,self.profiles.directory)
            self.drafts[draft['id']]=draft
            return preview

    def prepare_change(self, data):
        with self.lock:
            if self.job['status'] == 'running':
                raise ValueError('Дождитесь завершения текущей операции 1С.')
            profile = next((p for p in self.profiles.all() if p['id'] == self.active_connection), None)
            if not profile or not self.catalog:
                raise ValueError('Загрузка доступна для проекта, связанного с подключением 1С.')
            draft, preview = prepare(self.catalog, data.get('file_id'), data.get('content'), data.get('base_hash'), profile, self.profiles.directory)
            self.drafts[draft['id']] = draft
            return preview

    def apply_change(self, data):
        if not isinstance(data.get('id'),str):
            raise ValueError('Некорректный идентификатор изменения.')
        password = data.get('password','')
        if not isinstance(password,str) or len(password)>2048 or any(c in password for c in '\x00\r\n'):
            raise ValueError('Некорректный пароль.')
        with self.lock:
            if self.job['status'] == 'running':
                raise ValueError('Дождитесь завершения текущей операции 1С.')
            draft = self.drafts.get(data.get('id')) or self.change_sets.prepared(data.get('id'))
            if not draft or draft['status'] != 'prepared':
                raise ValueError('Заново подготовьте и просмотрите изменения перед загрузкой.')
            if not self.catalog or draft['root'] != str(self.catalog.root) or draft['connection'] != self.active_connection:
                raise ValueError('Проект обновился после подготовки. Сравните черновик с новой версией.')
            batch = draft.get('operation') == 'change_set'
            if batch: self.change_sets.check_package(draft, self.catalog)
            creating=draft.get('operation') in {'create_object', 'change_set'}
            base_hash=text_hash(read_text(self.catalog.root/'Configuration.xml')) if creating else self.catalog.file(draft['file_id'])['hash']
            if base_hash != draft['base_hash']:
                raise ValueError('Configuration.xml изменился после подготовки объекта.' if creating else 'Исходный модуль изменился после подготовки.')
            profile = next((p for p in self.profiles.all() if p['id'] == self.active_connection), None)
            if not profile:
                raise ValueError('Подключение к 1С не найдено.')
            if not profile.get('os_auth'):
                password = password or self.passwords.get(profile['id'],'')
                if profile.get('user') and not password:
                    raise ValueError('Введите пароль подключения 1С.')
                if password:
                    self.passwords[profile['id']] = password
            else:
                password = ''
            draft['status'] = 'submitting'
            if batch: self.change_sets.mark(draft, 'submitting')
            self.job = {'status':'running','operation':'import','phase':'Проверка изменений в 1С','cancellable':False,
                        'draft':draft['id'],'path':draft['path'],'imported':False,'log':''}
        def update(**changes):
            with self.lock:
                self.job.update(changes)
        def progress(phase, done, total):
            update(phase=phase,done=done,total=total,cancellable=False)
        def run():
            imported = False
            write_started = False
            terminal = {}
            try:
                if configuration_changed(profile,password,draft['root'],lambda **changes:update(**{**changes,'cancellable':False})):
                    raise ValueError('В Конфигураторе есть сохранённые изменения после выгрузки. Сначала обновите проект и сравните черновик; загрузка не выполнялась.')
                write_started = True
                update(write_started=True)
                if batch:
                    from athena.change_sets import upload_set
                    upload_set(profile,password,draft,update)
                elif creating:
                    upload_object(profile,password,draft,update)
                else:
                    upload_module(profile,password,draft,update)
                imported = True
                update(imported=True,phase='Набор загружен. Проверка результата выгрузкой' if batch else 'Объект загружен. Проверка результата выгрузкой' if creating else 'Модуль загружен. Проверка результата выгрузкой')
                destination = export_configuration(profile,password,self.profiles.directory,lambda **changes:update(**{**changes,'cancellable':False}))
                result = Catalog(destination).scan(progress)
                if batch:
                    from athena.change_sets import verify_set
                    verify_set(result,draft)
                elif creating:
                    verify_object(result,draft)
                else:
                    actual = result.file(draft['file_id'])['hash']
                    if actual != draft['content_hash']:
                        raise ValueError('Текст выгруженного из 1С модуля не совпал с черновиком. Проверьте результат в Конфигураторе.')
                self.index(destination,progress,profile['id'],catalog=result)
                self.profiles.save(profile,destination)
                draft['status'] = 'applied'
                terminal = dict(status='ready',phase='Набор загружен и проверен' if batch else 'Объект создан и проверен' if creating else 'Модуль загружен и проверен',cancellable=False)
            except Exception as exc:
                draft['status'] = 'needs_review' if write_started else 'failed'
                self.auto.update(enabled=False,phase='Пауза после ошибки загрузки',error='')
                message = str(exc).replace(password,'••••') if password else str(exc)
                if imported:
                    message = '1С подтвердила загрузку, но проверка результата не завершилась. ' + message
                elif write_started:
                    message = 'Загрузка в 1С была запущена. Проверьте состояние модуля перед повторной попыткой. ' + message
                terminal = dict(status='error',error=message,imported=imported,cancellable=False)
            finally:
                manifest = Path(draft['folder']) / 'manifest.json'
                try:
                    if batch: self.change_sets.mark(draft, draft['status'])
                    manifest.write_text(json.dumps(draft,ensure_ascii=False,indent=2),encoding='utf-8')
                except OSError:
                    terminal.update(status='error',error='Не удалось записать журнал изменения. Проверьте результат загрузки в 1С.',imported=imported)
                update(**terminal)
        threading.Thread(target=run,daemon=True).start()

    def configure_auto(self, data):
        enabled = data.get('enabled')
        interval = data.get('interval', 60)
        password = data.get('password', '')
        if not isinstance(password, str) or len(password) > 2048 or any(c in password for c in '\x00\r\n'):
            raise ValueError('Некорректный пароль.')
        if not isinstance(enabled, bool) or type(interval) is not int or not 30 <= interval <= 600:
            raise ValueError('Интервал автообновления должен быть от 30 до 600 секунд.')
        with self.lock:
            if enabled:
                profile = next((p for p in self.profiles.all() if p['id'] == self.active_connection), None)
                if not profile or not self.catalog:
                    raise ValueError('Сначала выгрузите конфигурацию из 1С или откройте её последний снимок.')
                if profile.get('user') and not profile.get('os_auth') and profile['id'] not in self.passwords and not password:
                    raise ValueError('Введите пароль подключения ниже. Он будет храниться только в памяти Athena.')
                if not (self.catalog.root / 'ConfigDumpInfo.xml').is_file():
                    raise ValueError('Нужен ConfigDumpInfo.xml. Выполните полную выгрузку из 1С.')
                if password and not profile.get('os_auth'):
                    self.passwords[profile['id']] = password
            self.auto.update(enabled=enabled, interval=interval, error='', phase='Ожидание проверки' if enabled else 'Выключено')
            if not enabled and self.job.get('operation') == 'autosync' and self.job.get('cancellable'):
                self.cancel_export.set()
            if enabled and self.auto_thread is None:
                self.auto_thread = threading.Thread(target=self.watch, daemon=True)
                self.auto_thread.start()
        self.auto_wake.set()
        return dict(self.auto)

    def watch(self):
        while not self.auto_stop.is_set():
            self.auto_wake.wait(self.auto['interval'])
            self.auto_wake.clear()
            if self.auto_stop.is_set():
                return
            if self.auto['enabled']:
                self.auto_tick()

    def auto_tick(self):
        with self.lock:
            if not self.auto['enabled'] or self.job['status'] == 'running':
                return
            profile = next((p for p in self.profiles.all() if p['id'] == self.active_connection), None)
            if not profile or not self.catalog:
                self.auto.update(enabled=False, phase='Выключено: проект без подключения')
                return
            password = self.passwords.get(profile['id'], '')
            reference = self.catalog.root
            self.cancel_export.clear()
            self.job = {'status': 'running', 'operation': 'autosync', 'phase': 'Проверка изменений', 'cancellable': True}
            self.auto.update(phase='Проверка изменений', error='')
        def update(**changes):
            with self.lock:
                self.job.update(changes)
        def progress(phase, done, total):
            update(phase=phase, done=done, total=total, cancellable=False)
        try:
            changed = configuration_changed(profile, password, reference, update, self.cancel_export)
            if self.cancel_export.is_set() or not self.auto['enabled']:
                raise ExportCancelled('Автообновление выключено.')
            if changed:
                self.auto['phase'] = 'Обновление выгрузки'
                destination = export_configuration(profile, password, self.profiles.directory, update, self.cancel_export)
                if self.cancel_export.is_set():
                    raise ExportCancelled('Автообновление выключено.')
                self.index(destination, progress, profile['id'])
                self.profiles.save(profile, destination)
            update(status='ready', cancellable=False)
            self.auto.update(last_check=time.time(), phase='Обновлено' if changed else 'Изменений нет', error='')
        except ExportCancelled:
            update(status='ready', cancellable=False)
            self.auto['phase'] = 'Выключено' if not self.auto['enabled'] else 'Проверка отменена'
        except Exception as exc:
            message = str(exc).replace(password, '••••') if password else str(exc)
            update(status='ready', cancellable=False)
            self.auto.update(last_check=time.time(), phase='Ошибка проверки', error=message)

    def close(self):
        self.documentation.close()
        self.code_agent.close()
        self.ai.close()
        self.auto_stop.set()
        self.auto_wake.set()
        self.cancel_export.set()
        with self.lock:
            self.semantic_enabled=False
            worker=self.semantic_worker
        if worker:worker.join(timeout=3)
        close=getattr(self.embedder,'close',None)
        if close:close()

    def available_platforms(self):
        found=platforms()
        for profile in self.profiles.all():
            try:path=validate_platform(profile.get('platform',''))
            except (ValueError,OSError):continue
            if path not in found:found.append(path)
        return found

    def connections(self):
        return {'profiles': [{**p, 'has_password': p['id'] in self.passwords} for p in self.profiles.all()],
                'bases': registered_bases(), 'platforms': self.available_platforms(), 'preferred_platform':self.user_profile().get('platform',''), 'active': self.active_connection}

    def index(self, path, progress, connection=None, catalog=None):
        with self.lock:
            self.job.update(index_started=time.time(),source=str(path))
        catalog = catalog or Catalog(Path(path).expanduser()).scan(progress)
        active_connection = connection or next((p['id'] for p in self.profiles.all() if p.get('last_export') and Path(p['last_export']).resolve() == catalog.root), None)
        snapshot = self.knowledge.index(catalog, 'connection:'+active_connection if active_connection else 'folder:'+os.path.normcase(str(catalog.root)), progress)
        recent = [str(catalog.root)] + [p for p in self.recent() if p != str(catalog.root)]
        try:
            STATE.parent.mkdir(exist_ok=True)
            STATE.write_text(json.dumps(recent[:8], ensure_ascii=False), encoding='utf-8')
        except OSError:
            pass
        with self.lock:
            self.catalog = catalog
            self.knowledge_snapshot = snapshot
            self.active_connection = active_connection
            self.revision += 1
        self.schedule_embeddings()

    def recent(self):
        try:
            return json.loads(STATE.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return []

    def load(self, path):
        if path == '__DEMO__':
            path = str(BASE / 'examples' / 'demo')
        with self.lock:
            if self.job['status'] == 'running':
                raise ValueError('Дождитесь завершения загрузки текущего проекта.')
            self.job = {'status': 'running', 'phase': 'Сканирование файлов', 'done': 0, 'total': 0, 'source': str(path)}
        def progress(phase, done, total):
            with self.lock:
                self.job.update(phase=phase, done=done, total=total)
        def run():
            try:
                self.index(path, progress)
                with self.lock:
                    self.job = {'status': 'ready'}
            except Exception as exc:
                with self.lock:
                    self.job.update(status='error', error=str(exc))
        threading.Thread(target=run, daemon=True).start()

    def export(self, data):
        profile = normalize(data)
        password = data.get('password', '')
        if not isinstance(password, str) or len(password) > 2048 or any(c in password for c in '\x00\r\n'):
            raise ValueError('Некорректный пароль.')
        with self.lock:
            if self.job['status'] == 'running':
                raise ValueError('Дождитесь завершения текущей операции.')
            previous = next((p for p in self.profiles.all() if p.get('id') == profile['id']), {})
            if previous.get('user', '') != profile['user'] or previous.get('os_auth', False) != profile['os_auth']:
                self.passwords.pop(profile['id'], None)
            self.profiles.save(profile)
            if profile['os_auth']:
                password = ''
                self.passwords.pop(profile['id'], None)
            elif password:
                self.passwords[profile['id']] = password
            else:
                password = self.passwords.get(profile['id'], '')
            self.cancel_export.clear()
            self.job = {'status': 'running', 'operation': 'export', 'connection': profile['id'], 'phase': 'Запуск 1С', 'done': 0, 'total': 0, 'elapsed': 0, 'cancellable': True, 'log': ''}
        def update(**changes):
            with self.lock:
                self.job.update(changes)
        def progress(phase, done, total):
            update(phase=phase, done=done, total=total, cancellable=False)
        def run():
            try:
                destination = export_configuration(profile, password, self.profiles.directory, update, self.cancel_export)
                if self.cancel_export.is_set():
                    raise ExportCancelled('Выгрузка отменена. Предыдущий проект остаётся открытым.')
                self.index(destination, progress, profile['id'])
                self.profiles.save(profile, destination)
                update(status='ready', cancellable=False)
            except ExportCancelled as exc:
                update(status='cancelled', error=str(exc), cancellable=False)
            except Exception as exc:
                # Never expose a subprocess command or an unmasked password in errors.
                message = str(exc).replace(password, '••••') if password else str(exc)
                update(status='error', error=message, cancellable=False)
        threading.Thread(target=run, daemon=True).start()


def choose_folder(purpose='project'):
    # Tk runs in a dedicated process: its UI never crosses HTTP worker threads.
    import subprocess
    import sys
    scripts = {
        'project': "p=filedialog.askdirectory(title='Выберите папку с Configuration.xml')",
        'base': "p=filedialog.askdirectory(title='Выберите папку базы с 1Cv8.1CD')",
        'platform': "p=filedialog.askopenfilename(title='Выберите 1cv8.exe', filetypes=[('Платформа 1С', '1cv8.exe')])",
    }
    if purpose not in scripts:
        raise ValueError('Неизвестный тип выбора файла.')
    script = "import tkinter as t; from tkinter import filedialog; r=t.Tk(); r.withdraw(); r.attributes('-topmost', True); " + scripts[purpose] + "; print(p); r.destroy()"
    result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, encoding='utf-8',
                            env={**__import__('os').environ, 'PYTHONIOENCODING': 'utf-8'})
    if result.returncode:
        raise ValueError('Не удалось открыть окно выбора папки. Вставьте путь к выгрузке в поле проекта.')
    return result.stdout.strip()


def handler(workspace):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def respond(self, payload, status=200):
            data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(data)

        def authorized(self):
            return secrets.compare_digest(self.headers.get('X-Athena-Token', ''), workspace.token)

        def do_GET(self):
            route = urlparse(self.path)
            if route.path.startswith('/api/'):
                if not self.authorized():
                    return self.respond({'error': 'Нет доступа.'}, 403)
                try:
                    if route.path == '/api/onboarding':
                        return self.respond(workspace.onboarding_status())
                    if route.path == '/api/gpu-install':
                        return self.respond(workspace.gpu_installer.status())
                    if route.path == '/api/ai/settings':
                        return self.respond(workspace.ai.status())
                    if route.path == '/api/ai/job':
                        return self.respond(workspace.ai.job(parse_qs(route.query).get('id',[''])[0]))
                    if route.path == '/api/state':
                        with workspace.lock:
                            job=dict(workspace.job)
                        if job.get('status')=='running' and job.get('index_started'):
                            job['elapsed']=int(time.time()-job['index_started'])
                        return self.respond({'job': job, 'recent': workspace.recent(), 'revision': workspace.revision, 'auto': dict(workspace.auto)})
                    if route.path == '/api/connections':
                        return self.respond(workspace.connections())
                    if route.path == '/api/completion/settings':
                        return self.respond(workspace.completion.status())
                    if route.path == '/api/change-sets':
                        project, _, _ = workspace.set_context()
                        return self.respond({'sets': workspace.change_sets.list(project)})
                    if route.path == '/api/documents':
                        return self.respond(workspace.documentation.listing())
                    if route.path == '/api/documents/job':
                        return self.respond(workspace.documentation.read(parse_qs(route.query).get('id',[''])[0]))
                    if route.path == '/api/agent/tools':
                        from athena.agent_tools import definitions
                        from athena.code_agent import tool_catalog
                        return self.respond({'tools': definitions(), 'protocol_version': 1,
                            'modes':{mode:[t['function']['name'] for t in tool_catalog(mode)] for mode in ['analyze','propose']},
                            'automatic':['create_change_set']})
                    if route.path == '/api/agent/context':
                        return self.respond(workspace.agent_tools.context())
                    if route.path == '/api/agent/history':
                        return self.respond({'calls': workspace.agent_tools.history()})
                    if route.path == '/api/agent/status':
                        return self.respond(workspace.code_agent.status())
                    if route.path == '/api/agent/job':
                        return self.respond(workspace.code_agent.job(parse_qs(route.query).get('id',[''])[0]))
                    if route.path == '/api/embedding-settings':
                        with workspace.lock:
                            result={'settings':dict(workspace.embedding_settings),'enabled':workspace.semantic_enabled,'running':bool(workspace.semantic_worker and workspace.semantic_worker.is_alive())}
                        return self.respond(result)
                    if route.path in {'/api/knowledge','/api/search-functions','/api/function'}:
                        with workspace.lock:
                            snapshot=workspace.knowledge_snapshot
                        if route.path == '/api/knowledge':
                            return self.respond(workspace.knowledge_status())
                        if not snapshot:
                            raise ValueError('Сначала откройте проект и дождитесь индексации.')
                        query=parse_qs(route.query)
                        if route.path == '/api/function':
                            return self.respond(workspace.knowledge.symbol(snapshot,query.get('id',[''])[0]))
                        return self.respond(workspace.search_functions(query.get('q',[''])[0],query.get('mode',['hybrid'])[0]))
                    if route.path == '/api/project':
                        return self.respond(workspace.catalog.summary() if workspace.catalog else None)
                    if route.path == '/api/file':
                        if not workspace.catalog:
                            raise ValueError('Сначала откройте проект.')
                        return self.respond(workspace.catalog.file(parse_qs(route.query).get('id', [''])[0]))
                    return self.respond({'error': 'Неизвестный адрес.'}, 404)
                except (ValueError, OSError, UnicodeError) as exc:
                    return self.respond({'error': str(exc)}, 400)
            allowed = {'/': ('index.html', 'text/html'), '/setup': ('setup.html','text/html'), '/setup.js':('setup.js','text/javascript'), '/setup.css':('setup.css','text/css'), '/app.js': ('app.js', 'text/javascript'), '/completion.js':('completion.js','text/javascript'), '/style.css': ('style.css', 'text/css'), '/export.css': ('export.css', 'text/css')}
            allowed['/change-sets.js'] = ('change-sets.js', 'text/javascript')
            allowed['/agent-tools'] = ('agent-tools.html', 'text/html')
            allowed['/agent-tools.js'] = ('agent-tools.js', 'text/javascript')
            allowed['/documentation.js'] = ('documentation.js', 'text/javascript')
            allowed['/code-agent.js'] = ('code-agent.js', 'text/javascript')
            if route.path not in allowed:
                return self.respond({'error': 'Не найдено.'}, 404)
            name, mime = allowed[route.path]
            data = (WEB / name).read_text(encoding='utf-8').replace('__ATHENA_TOKEN__', workspace.token).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', mime + '; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Frame-Options', 'DENY')
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            authorized = self.authorized()
            try:
                size = int(self.headers.get('Content-Length', 0))
                if size < 0 or size > 4_000_000:
                    if not authorized:
                        return self.respond({'error': 'Нет доступа.'}, 403)
                    raise ValueError('Слишком большой запрос.')
                # Closing with unread request bytes can reset the TCP connection on
                # Windows before the client receives the 403. Drain only a bounded
                # body, without parsing it or executing any unauthenticated action.
                if not authorized:
                    self.connection.settimeout(2)
                    try:
                        self.rfile.read(size)
                    except OSError:
                        pass
                    return self.respond({'error': 'Нет доступа.'}, 403)
                body = json.loads(self.rfile.read(size) or b'{}')
                if not isinstance(body, dict):
                    raise ValueError('Ожидается объект параметров.')
                if self.path == '/api/onboarding':
                    return self.respond(workspace.complete_onboarding(body))
                if self.path == '/api/completion/settings':
                    return self.respond(workspace.completion.configure(body))
                if self.path == '/api/completion':
                    with workspace.lock: snapshot = workspace.knowledge_snapshot
                    return self.respond(workspace.completion.suggest(body, snapshot))
                if self.path == '/api/change-sets':
                    return self.respond(workspace.change_set_action(body))
                if self.path == '/api/agent/call':
                    return self.respond(workspace.agent_tools.call(body))
                if self.path == '/api/documents/start':
                    return self.respond(workspace.documentation.start(body),202)
                if self.path == '/api/documents/cancel':
                    return self.respond(workspace.documentation.cancel(body.get('id')))
                if self.path == '/api/documents/save':
                    return self.respond(workspace.documentation.save(body))
                if self.path == '/api/agent/start':
                    return self.respond(workspace.code_agent.start(body),202)
                if self.path == '/api/agent/cancel':
                    return self.respond(workspace.code_agent.cancel(body.get('id')))
                if self.path == '/api/agent/settings':
                    return self.respond(workspace.code_agent.configure(body))
                if self.path == '/api/ai/test':
                    return self.respond(workspace.ai.test_connection(body))
                if self.path == '/api/platform/check':
                    return self.respond({'path':validate_platform(body.get('path','')),'message':'Файл 1cv8.exe найден.'})
                if self.path == '/api/gpu-install':
                    return self.respond(workspace.gpu_installer.start(),202)
                if self.path == '/api/ai/settings':
                    return self.respond(workspace.ai.configure(body))
                if self.path == '/api/ai/models':
                    return self.respond(workspace.ai.models())
                if self.path == '/api/ai/ask':
                    return self.respond(workspace.ai.start(body.get('question'),workspace.ai_context,workspace.knowledge,body.get('context_only',False)),202)
                if self.path == '/api/assistant/ask':
                    from athena.ai_metadata import inventory_request, structure_request
                    question = body.get('question')
                    if not isinstance(question,str) or not question.strip() or len(question)>2000:
                        raise ValueError('Введите вопрос до 2000 символов.')
                    if inventory_request(question) is not None or structure_request(question):
                        result=workspace.ai.start(question,workspace.ai_context,workspace.knowledge)
                        return self.respond({**result,'engine':'ai'},202)
                    context=workspace.agent_tools.context()
                    result=workspace.code_agent.start({'goal':question,'mode':'analyze',
                        'context':{k:context[k] for k in ['project','snapshot','workspace_revision']}})
                    return self.respond({**result,'engine':'agent'},202)
                if self.path == '/api/ai/cancel':
                    return self.respond(workspace.ai.cancel(body.get('id')))
                if self.path == '/api/embeddings':
                    return self.respond(workspace.set_semantic(body),202)
                if self.path == '/api/choose':
                    return self.respond({'path': choose_folder(body.get('purpose', 'project'))})
                if self.path == '/api/export':
                    workspace.export(body)
                    return self.respond({'status': 'running'}, 202)
                if self.path == '/api/auto':
                    return self.respond(workspace.configure_auto(body))
                if self.path == '/api/prepare-change':
                    return self.respond(workspace.prepare_change(body))
                if self.path == '/api/prepare-object':
                    return self.respond(workspace.prepare_object(body))
                if self.path == '/api/apply-change':
                    workspace.apply_change(body)
                    return self.respond({'status':'running'},202)
                if self.path == '/api/cancel':
                    with workspace.lock:
                        if not workspace.job.get('cancellable') or workspace.job['status'] != 'running':
                            raise ValueError('Сейчас нет выгрузки, которую можно отменить.')
                        workspace.cancel_export.set()
                    return self.respond({'status': 'cancelling'})
                if self.path == '/api/open':
                    path = body.get('path', '')
                    if not isinstance(path, str) or not path.strip():
                        raise ValueError('Укажите путь к папке выгрузки.')
                    workspace.load(path.strip())
                    return self.respond({'status': 'running'}, 202)
                return self.respond({'error': 'Неизвестный адрес.'}, 404)
            except (ValueError, OSError) as exc:
                self.respond({'error': str(exc)}, 400)
    return Handler


def main():
    parser = argparse.ArgumentParser(description='Athena — просмотр файловой выгрузки 1С')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--project', help='Каталог с Configuration.xml')
    args = parser.parse_args()
    workspace = Workspace()
    try:
        server = LocalServer(('127.0.0.1', args.port), handler(workspace))
    except OSError as exc:
        if not args.no_browser and not args.project:
            from urllib.request import urlopen
            url = f'http://127.0.0.1:{args.port}'
            try:
                with urlopen(url, timeout=2) as response:
                    existing = response.read(8192).decode('utf-8', errors='replace')
                if any(title in existing for title in ('<title>Athena · Обозреватель 1С</title>', '<title>Athena · IDE для 1С</title>')):
                    webbrowser.open(url)
                    print(f'Athena уже запущена: {url}', flush=True)
                    return
            except (OSError, UnicodeError):
                pass
        parser.exit(1, f'Не удалось запустить Athena на порту {args.port}: {exc}\nПопробуйте --port 8766.\n')
    if args.project:
        workspace.load(args.project)
    from athena.reference_server import ensure_service
    try:
        platform = workspace.user_profile().get('platform') or next((p.get('platform') for p in workspace.profiles.all() if p.get('platform')), '')
        ensure_service(STATE.parent, platform)
    except (OSError, ValueError) as exc:
        print('Не удалось открыть сервис справки: ' + str(exc), flush=True)
    url = f'http://127.0.0.1:{server.server_port}'
    print(f'Athena: {url}\nДля остановки нажмите Ctrl+C.', flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        workspace.close()
        server.server_close()


if __name__ == '__main__':
    main()
