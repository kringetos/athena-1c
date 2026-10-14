"""Prepare immutable, single-module changes and import selected BSL files."""
from __future__ import annotations

import difflib
import json
import os
import subprocess
import time
import uuid
from pathlib import Path

from athena.catalog import read_text, canonical, text_hash
from athena.exporter import command, redact


def prepare(catalog, file_id, content, expected_hash, profile, directory):
    if not isinstance(file_id,str) or not isinstance(expected_hash,str):
        raise ValueError('Укажите модуль и исходную версию текста.')
    if not isinstance(content, str) or len(content.encode('utf-8')) > 2_000_000 or '\x00' in content:
        raise ValueError('Модуль должен быть текстом BSL размером не более 2 МБ.')
    source = catalog.files.get(file_id)
    if source is None or source.suffix.lower() != '.bsl' or not source.resolve().is_relative_to(catalog.root):
        raise ValueError('Можно редактировать только BSL-модуль текущего проекта.')
    before = read_text(source)
    if expected_hash != text_hash(before):
        raise ValueError('Исходный файл изменился. Обновите проект и сравните черновик с новой версией.')
    if canonical(content) == canonical(before):
        raise ValueError('В модуле нет изменений для загрузки.')
    identity = uuid.uuid4().hex
    folder = Path(directory).resolve() / 'changes' / identity
    relative = source.relative_to(catalog.root)
    destination = folder / 'files' / relative
    destination.parent.mkdir(parents=True, exist_ok=False)
    raw = source.read_bytes()
    (folder / 'before.bsl').write_bytes(raw)
    edited = canonical(content).replace('\n', '\r\n' if '\r\n' in before else '\n')
    encoding = 'utf-16' if raw.startswith((b'\xff\xfe',b'\xfe\xff')) else 'utf-8-sig'
    destination.write_bytes(edited.encode(encoding))
    difference = ''.join(difflib.unified_diff(canonical(before).splitlines(keepends=True), canonical(content).splitlines(keepends=True),
                                           fromfile=relative.as_posix(), tofile=relative.as_posix(), lineterm='\n'))
    draft = {'id':identity, 'root':str(catalog.root), 'file_id':file_id, 'path':relative.as_posix(),
             'base_hash':text_hash(before), 'content_hash':text_hash(content), 'connection':profile['id'],
             'folder':str(folder), 'status':'prepared', 'created':time.time()}
    (folder / 'manifest.json').write_text(json.dumps(draft,ensure_ascii=False,indent=2),encoding='utf-8')
    return draft, {'id':identity, 'path':draft['path'], 'connection':profile['name'],
                   'extension':profile.get('extension',''), 'diff':difference[:200_000], 'truncated':len(difference)>200_000,
                   'backup':str(folder / 'before.bsl')}


def import_command(profile, password, folder, module, log, result):
    # Reuse authentication only. A selected external property is the entire write scope.
    args = command(profile,password,folder,log,result)
    start = args.index('/DumpConfigToFiles')
    end = args.index('/Out')
    operation = ['/LoadConfigFromFiles',str(folder),'-Format','Hierarchical','-partial','-files',str(module)]
    if profile.get('extension'):
        operation.extend(['-Extension',profile['extension']])
    return args[:start] + operation + args[end:]


def upload_module(profile, password, draft, progress=lambda **kwargs:None, timeout=300):
    folder = Path(draft['folder'])
    module = folder / 'files' / draft['path']
    if not module.resolve().is_relative_to(folder.resolve()) or text_hash(read_text(module)) != draft['content_hash']:
        raise ValueError('Подготовленный модуль изменился. Заново подготовьте загрузку.')
    log, result = folder / 'import.log', folder / 'result.txt'
    args = import_command(profile,password,folder/'files',module,log,result)
    run_import(args,password,log,result,progress,'Загрузка модуля в конфигурацию 1С',timeout)


def run_import(args,password,log,result,progress,phase,timeout):
    process = None
    started = time.monotonic()
    try:
        process = subprocess.Popen(args,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                                   shell=False,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        while process.poll() is None:
            progress(phase=phase,elapsed=int(time.monotonic()-started),cancellable=False)
            if time.monotonic()-started >= timeout:
                raise ValueError('1С не завершила загрузку за 5 минут. Результат нужно проверить в Конфигураторе.')
            time.sleep(.4)
        safe_log = redact(read_text(log),password) if log.is_file() else ''
        progress(log=safe_log[-6000:])
        code = read_text(result).strip() if result.is_file() else ''
        if process.returncode != 0 or code != '0':
            raise ValueError(f'1С не подтвердила загрузку модуля (процесс {process.returncode}, результат {code or "не получен"}). Проверьте журнал и состояние конфигурации.')
    finally:
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=5)
        if log.is_file():
            log.write_text(redact(read_text(log),password),encoding='utf-8')
