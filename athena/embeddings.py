"""Local configurable CPU/CUDA embeddings. Network access only downloads pinned public model files."""
import hashlib
import threading
import os
import time
from pathlib import Path

MODEL = 'Xenova/multilingual-e5-small'
REVISION = '761b726dd34fb83930e26aab4e9ac3899aa1fa78'
RECIPE = 'e5-q8-batched-lengthsort-v1-mean-pool-overflow512-stride64-context2'
DIMENSIONS = 384

def settings(data=None):
    data = data or {}
    device = data.get('device', 'cpu')
    mode = data.get('mode', 'comfort')
    if device not in {'cpu', 'gpu'} or mode not in {'comfort', 'fast'}:
        raise ValueError('Неизвестное устройство или режим индексации.')
    defaults = {'threads': min(2, os.cpu_count() or 1), 'batch_size': 8, 'token_budget': 2048, 'gpu_memory_mb': 2048, 'pause_ms': 100} if mode == 'comfort' else {'threads': min(os.cpu_count() or 4, 8), 'batch_size': 32, 'token_budget': 8192, 'gpu_memory_mb': 4096, 'pause_ms': 0}
    limits = {'threads': (1, min(os.cpu_count() or 1, 32)), 'batch_size': (1, 64), 'token_budget': (512, 32768), 'gpu_memory_mb': (256, 32768), 'pause_ms': (0, 2000)}
    result = {'device': device, 'mode': mode}
    for key, default in defaults.items():
        value = data.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not limits[key][0] <= value <= limits[key][1]:
            raise ValueError('Недопустимое значение настройки: ' + key)
        result[key] = value
    return result


class LocalEmbeddings:
    name = 'multilingual-e5-small · CPU'
    provider = 'local-onnx'
    model = MODEL
    version = REVISION + ':' + RECIPE
    dimensions = DIMENSIONS
    model_id = hashlib.sha256((MODEL + ':' + version).encode()).hexdigest()

    def __init__(self, directory, config=None):
        self.config = settings(config)
        if self.config['device'] == 'gpu':
            self.version = REVISION + ':' + RECIPE.replace('q8', 'fp32')
            self.model_id = hashlib.sha256((MODEL + ':' + self.version).encode()).hexdigest()
        self.name = 'multilingual-e5-small · ' + self.config['device'].upper()
        self.directory = Path(directory)
        self.lock = threading.RLock()
        self.cancel = threading.Event()
        self.session = None
        self.tokenizer = None

    def prepare(self):
        try:
            self._prepare()
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError('Не удалось подготовить локальную модель: '+str(exc)) from exc

    def _prepare(self):
        with self.lock:
            if self.session is not None:
                return
            os.environ.setdefault('HF_XET_CACHE',str(self.directory.parent/'xet-cache'))
            try:
                import onnxruntime as ort
                from tokenizers import Tokenizer
                from huggingface_hub import hf_hub_download
            except ImportError as exc:
                raise ValueError('Установите зависимости: python -m pip install -r requirements.txt') from exc
            self.directory.mkdir(parents=True, exist_ok=True)
            gpu = self.config['device'] == 'gpu'
            if gpu and hasattr(ort, 'preload_dlls'):ort.preload_dlls(directory='')
            if gpu and 'CUDAExecutionProvider' not in ort.get_available_providers():
                raise ValueError('Ускорение GPU не настроено. Откройте мастер настройки рабочего места и нажмите «Настроить ускорение GPU», либо выберите CPU.')
            model_file = 'onnx/model.onnx' if gpu else 'onnx/model_quantized.onnx'
            files = {}
            for filename in ['tokenizer.json', model_file]:
                try:
                    files[filename] = hf_hub_download(MODEL, filename, revision=REVISION,
                                                      cache_dir=str(self.directory),token=False,local_files_only=True)
                except OSError:
                    files[filename] = hf_hub_download(MODEL, filename, revision=REVISION,
                                                      cache_dir=str(self.directory),token=False)
            tokenizer = Tokenizer.from_file(files['tokenizer.json'])
            tokenizer.enable_truncation(max_length=512, stride=64)
            tokenizer.enable_padding(pad_id=1, pad_token='<pad>')
            options = ort.SessionOptions()
            options.intra_op_num_threads = self.config['threads']
            options.inter_op_num_threads = 1
            providers = [('CUDAExecutionProvider', {'gpu_mem_limit': self.config['gpu_memory_mb'] * 1024 * 1024}), 'CPUExecutionProvider'] if gpu else ['CPUExecutionProvider']
            session = ort.InferenceSession(files[model_file], options, providers=providers)
            if gpu and 'CUDAExecutionProvider' not in session.get_providers():
                raise ValueError('CUDA не запустилась. Проверьте драйвер и CUDA/cuDNN либо выберите CPU.')
            self.tokenizer, self.session = tokenizer, session

    def _embed(self, texts, prefix):
        import numpy as np
        self.prepare()
        with self.lock:
            pooled = [[] for _ in texts]
            windows = []
            for index, text in enumerate(texts):
                encoded = self.tokenizer.encode(prefix + text)
                windows.extend((index, part) for part in [encoded] + encoded.overflowing)
            windows.sort(key=lambda item: len(item[1].ids))
            offset = 0
            token_types = any(i.name == 'token_type_ids' for i in self.session.get_inputs())
            while offset < len(windows):
                if prefix == 'passage: ' and self.cancel.is_set():
                    raise ValueError('Смысловая индексация приостановлена.')
                batch = []
                while offset < len(windows):
                    width = len(windows[offset][1].ids)
                    if batch and (len(batch) >= self.config['batch_size'] or width * (len(batch) + 1) > self.config['token_budget']):
                        break
                    batch.append(windows[offset]); offset += 1
                width = max(len(part.ids) for _, part in batch)
                ids = np.full((len(batch), width), 1, dtype=np.int64)
                mask = np.zeros_like(ids)
                for row, (_, part) in enumerate(batch):
                    ids[row, :len(part.ids)] = part.ids
                    mask[row, :len(part.ids)] = part.attention_mask
                feeds = {'input_ids': ids, 'attention_mask': mask}
                if token_types: feeds['token_type_ids'] = np.zeros_like(ids)
                try:
                    hidden = self.session.run(None, feeds)[0]
                except Exception as exc:
                    raise ValueError('Ошибка вычисления эмбеддингов. Уменьшите пакет или бюджет токенов: ' + str(exc)) from exc
                values = (hidden * mask[:, :, None]).sum(axis=1) / mask.sum(axis=1)[:, None]
                for row, (index, _) in enumerate(batch): pooled[index].append(values[row])
                if prefix == 'passage: ' and self.config['pause_ms']: time.sleep(self.config['pause_ms'] / 1000)
            vectors = []
            for parts in pooled:
                value = np.mean(parts, axis=0).astype(np.float32)
                vectors.append(value / max(float(np.linalg.norm(value)), 1e-12))
            return vectors

    def documents(self, texts):
        return self._embed(texts, 'passage: ')

    def query(self, text):
        return self._embed([text], 'query: ')[0]
