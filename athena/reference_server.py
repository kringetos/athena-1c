"""Local reference UI; runs independently so project indexing keeps running."""
import argparse
import json
import secrets
import threading
import subprocess
import sys
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from athena.reference import Reference
from athena.language import LanguageKnowledge

BASE = Path(__file__).resolve().parent.parent


def ensure_service(directory, platform=''):
    """Start the independent reference service alongside the IDE."""
    from urllib.request import urlopen
    from urllib.error import URLError
    try:
        with urlopen('http://127.0.0.1:8767/', timeout=2) as response:
            if 'Athena · Справка платформы' not in response.read().decode('utf-8'):
                raise ValueError('Порт справки 8767 занят другим сервисом.')
        return
    except URLError: pass
    directory = Path(directory)
    arguments = [sys.executable, '-m', 'athena.reference_server']
    if platform: arguments += ['--platform', platform]
    with (directory / 'reference-server.log').open('a', encoding='utf-8') as output, (directory / 'reference-server-errors.log').open('a', encoding='utf-8') as errors:
        subprocess.Popen(arguments, cwd=str(BASE), stdout=output, stderr=errors,
                         creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


class ReferenceService:
    def __init__(self, database):
        self.reference = Reference(database); self.token = secrets.token_urlsafe(32)
        self.language = LanguageKnowledge(self.reference, Path(database).parent / 'knowledge.sqlite3')
        self.lock = threading.Lock(); self.job = {'status': 'idle'}; self.worker = None

    def status(self):
        with self.lock: job = dict(self.job)
        return {'job': job, 'sources': self.reference.sources()}

    def start(self, platform):
        if not isinstance(platform, str): raise ValueError('Укажите путь к 1cv8.exe.')
        with self.lock:
            if self.worker and self.worker.is_alive(): raise ValueError('Импорт справки уже выполняется.')
            self.job = {'status': 'running', 'phase': 'Проверка справки платформы'}
            def run():
                def progress(book, done, total):
                    with self.lock: self.job.update(phase=book, done=done, total=total)
                try:
                    result = self.reference.import_platform(platform, progress)
                    with self.lock: self.job = {'status': 'ready', 'source': result}
                except Exception as exc:
                    with self.lock: self.job = {'status': 'error', 'error': str(exc)}
            self.worker = threading.Thread(target=run, daemon=True); self.worker.start()


def handler(service):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass

        def send(self, data, code=200, mime='application/json'):
            raw = json.dumps(data, ensure_ascii=False).encode() if mime == 'application/json' else data.encode()
            self.send_response(code); self.send_header('Content-Type', mime + '; charset=utf-8')
            self.send_header('Content-Length', str(len(raw))); self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff'); self.end_headers(); self.wfile.write(raw)

        def permitted(self):
            if self.headers.get('Host') not in {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}:
                self.send({'error': 'Недопустимый адрес.'}, 403); return False
            return True

        def authenticated(self):
            if not secrets.compare_digest(self.headers.get('X-Athena-Token', ''), service.token):
                self.send({'error': 'Нет доступа.'}, 403); return False
            return True

        def do_GET(self):
            if not self.permitted(): return
            route = urlparse(self.path); query = parse_qs(route.query)
            if route.path == '/':
                html = (BASE / 'web' / 'reference.html').read_text(encoding='utf-8').replace('__ATHENA_TOKEN__', service.token)
                return self.send(html, mime='text/html')
            if not self.authenticated(): return
            try:
                if route.path == '/api/status': return self.send(service.status())
                if route.path == '/api/language/status': return self.send({'sources': service.reference.sources(), 'projects': service.language.projects()})
                if route.path.startswith('/api/language/'):
                    options = {key: query.get(key, [''])[0] for key in ['source', 'snapshot']}
                    if route.path == '/api/language/search': return self.send(service.language.search(query.get('q', [''])[0], **options, scope=query.get('scope', ['all'])[0], exact=query.get('exact', ['0'])[0] == '1'))
                    if route.path == '/api/language/item': return self.send(service.language.item(query.get('origin', [''])[0], query.get('id', [''])[0], **options))
                    if route.path == '/api/language/members': return self.send(service.language.members(query.get('owner', [''])[0], options['source']))
                if route.path == '/api/search': return self.send(service.reference.search(query.get('q', [''])[0], query.get('source', [''])[0]))
                if route.path == '/api/page': return self.send(service.reference.page(query.get('id', [''])[0]))
                self.send({'error': 'Не найдено.'}, 404)
            except (ValueError, OSError) as exc: self.send({'error': str(exc)}, 400)

        def do_POST(self):
            if not self.permitted() or not self.authenticated(): return
            try:
                if self.path != '/api/import': return self.send({'error': 'Не найдено.'}, 404)
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 8192: raise ValueError('Недопустимый размер запроса.')
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict): raise ValueError('Ожидается объект JSON.')
                service.start(body.get('platform')); self.send(service.status(), 202)
            except (ValueError, OSError) as exc: self.send({'error': str(exc)}, 400)
    return Handler


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--port', type=int, default=8767)
    parser.add_argument('--platform'); args = parser.parse_args()
    service = ReferenceService(BASE / '.athena' / 'reference.sqlite3')
    server = ThreadingHTTPServer(('127.0.0.1', args.port), handler(service))
    if args.platform: service.start(args.platform)
    print(f'Справка Athena: http://127.0.0.1:{args.port}/', flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__ == '__main__': main()
