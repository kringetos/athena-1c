"""Managed GPU dependencies and local subprocess inference; never replaces host Python."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import re

BASE=Path(__file__).resolve().parent.parent
GPU_PACKAGES=['onnxruntime-gpu[cuda,cudnn]==1.31.0','onnx==1.23.2','antlr4-python3-runtime==4.13.2','tokenizers==0.23.3','huggingface-hub==1.33.0','numpy>=1.26,<3']

def check_driver():
    import ctypes
    try:
        driver=ctypes.WinDLL(str(Path(os.environ.get('SystemRoot','C:/Windows'))/'System32'/'nvcuda.dll'))
        version=ctypes.c_int()
        if driver.cuInit(0)!=0 or driver.cuDriverGetVersion(ctypes.byref(version))!=0:
            raise ValueError('Драйвер NVIDIA не смог открыть CUDA. Обновите драйвер или используйте CPU.')
        if version.value<13000:
            raise ValueError('Для этой версии ускорителя нужен драйвер NVIDIA с поддержкой CUDA 13+. Обновите драйвер, затем повторите настройку; сейчас можно использовать CPU.')
    except OSError as exc:
        raise ValueError('Драйвер CUDA не найден. Установите драйвер NVIDIA, затем повторите настройку; сейчас можно использовать CPU.') from exc

def runtime_python(directory):
    return Path(directory)/'gpu-runtime'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')

def runtime_ready(directory):
    return runtime_python(directory).is_file() and (Path(directory)/'gpu-ready.json').is_file()

def download_progress(lines):
    result={}
    for line in reversed(lines):
        if line.startswith(('Installing collected packages:', 'Successfully installed')):
            if not result:return {'installing':True}
        match=re.fullmatch(r'Progress (\d+) of (\d+)',line.strip())
        if match and 'downloaded' not in result:
            current,total=map(int,match.groups())
            result.update(downloaded=current,total=total)
            if total:result['percent']=min(100,100*current/total)
        if line.startswith('Downloading '):
            result['download']=line[:200];break
    return result

class GPUInstaller:
    def __init__(self,directory,on_ready=None):
        self.on_ready=on_ready
        self.directory=Path(directory);self.lock=threading.Lock();self.worker=None
        self.log_start=0
        self.job={'status':'ready' if runtime_ready(directory) else 'idle','phase':'GPU готов' if runtime_ready(directory) else 'Ускорение на GPU не настроено'}
    def status(self):
        with self.lock:result=dict(self.job)
        if result.get('started'):result['elapsed']=int(result.get('finished',time.time())-result['started'])
        if result['status']=='running' and result.get('phase','').startswith('Загрузка'):
            try:
                with (self.directory/'gpu-install.log').open('rb') as log:
                    log.seek(0,2);log.seek(max(self.log_start,log.tell()-65536));lines=log.read().decode('utf-8',errors='replace').splitlines()
                result.update(download_progress(lines))
                if result.get('installing'):result['phase']='Установка скачанных библиотек'
            except OSError:pass
        return result
    def update(self,**values):
        with self.lock:self.job.update(values)
    def command(self,args,phase,timeout):
        stage=2 if phase.startswith('Создание') else 3 if phase.startswith('Загрузка') else 4
        self.update(phase=phase,stage=stage)
        temporary=self.directory/'tmp'/'gpu-install'
        temporary.mkdir(parents=True,exist_ok=True)
        environment={**os.environ,'TEMP':str(temporary.resolve()),'TMP':str(temporary.resolve())}
        with (self.directory/'gpu-install.log').open('a',encoding='utf-8') as log:
            result=subprocess.run(args,cwd=str(BASE),stdout=log,stderr=log,timeout=timeout,
                env=environment,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if result.returncode:raise ValueError(phase+' не завершена. Подробности в .athena/gpu-install.log.')
    def start(self):
        if os.name!='nt' or sys.version_info<(3,11) or sys.maxsize<=2**32:
            raise ValueError('Автоустановка GPU поддерживает Windows x64 и Python 3.11+. На этом компьютере используйте CPU.')
        with self.lock:
            if self.worker and self.worker.is_alive():return dict(self.job)
            if runtime_ready(self.directory):return dict(self.job)
            log=self.directory/'gpu-install.log'
            self.log_start=log.stat().st_size if log.exists() else 0
            self.job={'status':'running','phase':'Проверка видеокарты','stage':1,'started':time.time()}
            self.worker=threading.Thread(target=self.run,daemon=True);self.worker.start()
            return dict(self.job)
    def run(self):
        try:
            self.directory.mkdir(parents=True,exist_ok=True)
            result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',
                '(Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name) -join "|"'],
                capture_output=True,text=True,timeout=30,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            if result.returncode or 'nvidia' not in result.stdout.lower():
                raise ValueError('Видеокарта NVIDIA не найдена. Установка CUDA не поможет; выберите CPU.')
            check_driver()
            python=runtime_python(self.directory)
            if not python.is_file():self.command([sys.executable,'-m','venv',str(python.parent.parent)],'Создание отдельного окружения Athena',120)
            self.command([str(python),'-m','pip','--isolated','install','--cache-dir',str(self.directory/'pip-cache'),'--progress-bar','raw','--index-url','https://pypi.org/simple',*GPU_PACKAGES],
                'Загрузка и установка библиотек GPU — это может занять несколько минут',3600)
            try:self.command([str(python),'-m','athena.gpu_worker','--probe'],'Проверка вычисления на видеокарте',120)
            except Exception as exc:
                raise ValueError('Библиотеки установлены, но CUDA не запустилась. Проверьте или обновите драйвер NVIDIA. Подробности в .athena/gpu-install.log.') from exc
            path=self.directory/'gpu-ready.json';temporary=path.with_suffix('.tmp')
            temporary.write_text(json.dumps({'version':1,'installed':time.time(),'runtime':'onnxruntime-gpu-1.31.0'}),encoding='utf-8');temporary.replace(path)
            if self.on_ready:self.on_ready()
            self.update(status='ready',phase='Ускорение GPU настроено. Можно выбрать GPU.',finished=time.time())
        except Exception as exc:
            self.update(status='error',phase='Не удалось настроить GPU',error=str(exc),finished=time.time())

class ManagedGPU:
    def __init__(self,directory,config):
        from athena.embeddings import LocalEmbeddings
        identity=LocalEmbeddings(Path(directory)/'models',config)
        for key in ['name','provider','model','version','dimensions','model_id']:
            setattr(self,key,getattr(identity,key))
        self.config=config;self.directory=Path(directory);self.cancel=threading.Event();self.lock=threading.Lock();self.process=None
    def close(self):
        process=self.process
        if process is not None:
            if process.poll() is None:process.terminate()
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:process.kill();process.wait()
            if process.stdin:process.stdin.close()
            if process.stdout:process.stdout.close()
            self.process=None
    def embed(self,texts,prefix):
        with self.lock:
            if prefix=='passage: ' and self.cancel.is_set():raise ValueError('Смысловая индексация приостановлена.')
            if self.process is None or self.process.poll() is not None:
                with (self.directory/'gpu-worker.log').open('a',encoding='utf-8') as log:
                    self.process=subprocess.Popen([str(runtime_python(self.directory)),'-m','athena.gpu_worker'],cwd=str(BASE),
                        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=log,text=True,encoding='utf-8',bufsize=1,
                        env={**os.environ,'HF_XET_CACHE':str((self.directory/'xet-cache').resolve())},
                        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            try:
                process=self.process
                process.stdin.write(json.dumps({'texts':texts,'prefix':prefix,'config':self.config,'models':str(self.directory/'models')},ensure_ascii=False)+'\n')
                process.stdin.flush();line=process.stdout.readline()
                if not line:raise ValueError('Процесс GPU остановился. Проверьте gpu-worker.log или выберите CPU.')
                response=json.loads(line)
                if 'error' in response:raise ValueError(response['error'])
                return response['vectors']
            except (OSError,ValueError):
                self.close();raise
    def documents(self,texts):return self.embed(texts,'passage: ')
    def query(self,text):return self.embed([text],'query: ')[0]
