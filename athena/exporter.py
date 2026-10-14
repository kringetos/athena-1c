"""Batch export of a saved configuration via the installed 1C thick client."""
from __future__ import annotations

import configparser
import hashlib
import json
import os
import re
import subprocess
import time
import tempfile
import uuid
from pathlib import Path

from athena.catalog import read_text

DATA = Path(__file__).resolve().parent.parent / '.athena'
PROFILE_FIELDS = ('id', 'name', 'kind', 'location', 'platform', 'user', 'os_auth', 'extension', 'last_export')


def platforms():
    found = set()
    for variable in ('ProgramFiles', 'ProgramFiles(x86)', 'LOCALAPPDATA'):
        base = os.environ.get(variable)
        if not base:
            continue
        for folder in ('1cv8', 'Programs/1cv8'):
            root = Path(base) / folder
            for pattern in ('*/bin/1cv8.exe', 'bin/1cv8.exe'):
                found.update(str(p.resolve()) for p in root.glob(pattern) if p.is_file())
    def version(path):
        numbers = re.findall(r'\d+', Path(path).parent.parent.name)
        return tuple(int(n) for n in numbers)
    return sorted(found, key=version, reverse=True)


def registered_bases():
    """Read only File/Srvr/Ref from the current user's launcher list."""
    appdata = os.environ.get('APPDATA')
    path = Path(appdata) / '1C/1CEStart/ibases.v8i' if appdata else None
    if not path or not path.is_file():
        return []
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read_string(read_text(path))
    except (OSError, UnicodeError, configparser.Error):
        return []
    result = []
    for name in parser.sections():
        connection = parser.get(name, 'Connect', fallback='')
        parts = {m[1].lower(): m[2].replace('""', '"') for m in re.finditer(r'(File|Srvr|Ref)\s*=\s*"((?:""|[^"])*)"', connection, re.I)}
        if parts.get('file'):
            kind, location = 'file', parts['file']
        elif parts.get('srvr') and parts.get('ref'):
            kind, location = 'server', parts['srvr'] + '/' + parts['ref']
        else:
            continue
        result.append({'name': name, 'kind': kind, 'location': location})
    return result


def validate_platform(value):
    if not isinstance(value,str) or len(value)>2048 or any(c in value for c in '\x00\r\n"'):
        raise ValueError('Некорректный путь к платформе 1С.')
    exe=Path(value.strip()).expanduser().resolve()
    if exe.name.lower()!='1cv8.exe' or not exe.is_file():
        raise ValueError('Выберите 1cv8.exe установленной платформы (толстый клиент). 1cestart.exe и 1cv8c.exe не подходят.')
    return str(exe)

def normalize(data):
    result = {}
    for field in ('name', 'kind', 'location', 'platform', 'user', 'extension'):
        value = data.get(field, '')
        if not isinstance(value, str) or len(value) > 2048 or any(c in value for c in '\x00\r\n"'):
            raise ValueError('Некорректное значение поля подключения.')
        result[field] = value.strip()
    if result['kind'] not in {'file', 'server'}:
        raise ValueError('Выберите файловую или серверную базу.')
    if not result['location']:
        raise ValueError('Укажите папку базы или адрес сервер/имя_базы.')
    if result['kind'] == 'file':
        base = Path(result['location']).expanduser().resolve()
        if not base.is_dir() or not (base / '1Cv8.1CD').is_file():
            raise ValueError('В папке файловой базы не найден 1Cv8.1CD. Здесь нужна папка самой базы, а не выгрузки.')
        result['location'] = str(base)
    else:
        result['location'] = result['location'].replace('\\', '/')
        if not re.fullmatch(r'[^/\s;]+/[^/\s;]+', result['location']) or result['location'].startswith('/'):
            raise ValueError('Адрес серверной базы должен иметь вид сервер[:порт]/имя_базы.')
    result['platform'] = validate_platform(result['platform'])
    if any(result[field].startswith(('/', '-')) for field in ('user', 'extension')):
        raise ValueError('Имя пользователя и расширения не должны начинаться с / или -.')
    result['os_auth'] = bool(data.get('os_auth', False))
    if result['os_auth']:
        result['user'] = ''
    result['name'] = result['name'] or result['location']
    identity = json.dumps([result['kind'], result['location'].casefold(), result['extension']], ensure_ascii=False)
    result['id'] = hashlib.sha256(identity.encode()).hexdigest()[:20]
    return result


class Profiles:
    def __init__(self, data_dir=DATA):
        self.directory = Path(data_dir)
        self.path = self.directory / 'connections.json'

    def all(self):
        try:
            rows = json.loads(self.path.read_text(encoding='utf-8'))
            return [{k: row[k] for k in PROFILE_FIELDS if k in row} for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
        except (OSError, ValueError):
            return []

    def save(self, profile, last_export=None):
        rows = self.all()
        old = next((p for p in rows if p.get('id') == profile['id']), {})
        safe = {k: profile[k] for k in PROFILE_FIELDS if k in profile}
        safe['last_export'] = str(last_export) if last_export else old.get('last_export', '')
        self.directory.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps([safe] + [p for p in rows if p.get('id') != safe['id']], ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(self.path)


def command(profile, password, destination, log, result):
    args = [profile['platform'], 'DESIGNER', '/F' if profile['kind'] == 'file' else '/S', profile['location']]
    args.append('/WA+' if profile['os_auth'] else '/WA-')
    if not profile['os_auth']:
        if profile['user']:
            args.extend(['/N', profile['user']])
        if password:
            args.extend(['/P', password])
    args.extend(['/DumpConfigToFiles', str(destination), '-Format', 'Hierarchical'])
    if profile['extension']:
        args.extend(['-Extension', profile['extension']])
    args.extend(['/Out', str(log), '/DumpResult', str(result), '/DisableStartupDialogs', '/DisableStartupMessages'])
    return args


def redact(text, password):
    if password:
        for spelling in {password, password.replace('"', '""'), password.replace('"', '\\"')}:
            text = text.replace(spelling, '••••')
    return text


class ExportCancelled(Exception):
    pass


def export_configuration(profile, password, data_dir=DATA, progress=lambda **kwargs: None, cancel=None, timeout=3600, reference=None):
    run = Path(data_dir).resolve() / 'exports' / profile['id'] / uuid.uuid4().hex
    destination = run / 'configuration'
    destination.mkdir(parents=True, exist_ok=False)
    log, result = run / '1c.log', run / 'result.txt'
    args = command(profile, password, destination, log, result)
    changes = run / 'changes.txt'
    if reference is not None:
        # Keep all output in this run, even if the platform fails mid-operation.
        # Sub-options must precede the next batch command (/Out).
        position = args.index('/Out')
        args[position:position] = ['-getChanges', str(changes),
                                   '-configDumpInfoForChanges', str(Path(reference) / 'ConfigDumpInfo.xml')]
    started = time.monotonic()
    process = None
    try:
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        try:
            process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       shell=False, creationflags=flags)
        except OSError:
            raise ValueError('Не удалось запустить 1cv8.exe. Проверьте путь и установку платформы.') from None
        while process.poll() is None:
            elapsed = int(time.monotonic() - started)
            progress(phase='Проверка сохранённых изменений в 1С' if reference else 'Выгрузка конфигурации из 1С', elapsed=elapsed, cancellable=True)
            if cancel and cancel.wait(.4):
                raise ExportCancelled('Выгрузка отменена. Предыдущий проект остаётся открытым.')
            if not cancel:
                time.sleep(.4)
            if elapsed >= timeout:
                raise ValueError(f'Операция 1С не завершилась за {timeout} секунд. Проверьте доступность базы, лицензию и блокировки Конфигуратора.')
        log_text = redact(read_text(log), password) if log.is_file() else ''
        result_code = read_text(result).strip() if result.is_file() else ''
        output_exists = changes.is_file() if reference else (destination / 'Configuration.xml').is_file()
        if process.returncode != 0 or result_code != '0' or not output_exists:
            progress(log=log_text[-6000:])
            raise ValueError(f'1С не завершила выгрузку успешно (код процесса {process.returncode}, результат {result_code or "не получен"}). Проверьте журнал, права пользователя, лицензию и доступность базы.')
        if reference is not None:
            return bool(read_text(changes).lstrip('\ufeff').strip())
        progress(log=log_text[-6000:], phase='Выгрузка завершена. Чтение метаданных', cancellable=False)
        return destination
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if log.is_file():
            safe_log = redact(read_text(log), password)
            log.write_text(safe_log, encoding='utf-8')
            progress(log=safe_log[-6000:])


def configuration_changed(profile, password, reference, progress=lambda **kwargs: None, cancel=None):
    reference = Path(reference)
    if not (reference / 'ConfigDumpInfo.xml').is_file():
        raise ValueError('Для автообновления нужна полная выгрузка с ConfigDumpInfo.xml. Выполните «Выгрузить снова».')
    with tempfile.TemporaryDirectory(prefix='athena-check-') as temporary:
        return export_configuration(profile, password, temporary, progress, cancel, timeout=120, reference=reference)
