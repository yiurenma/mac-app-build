"""Offline AI: one-click setup from the Settings page.

Everything runs on this Mac. Installing does two things:
1. installs the llama.cpp runtime (llama-cpp-python) into this tool's own .venv through the
   company's existing pip configuration (the same index check install.sh uses: no silent
   fallback to public PyPI);
2. downloads one model file (GGUF) from Hugging Face and checks its SHA-256 against the
   value Hugging Face publishes for that file.

If the download site is blocked, the person downloads the file elsewhere and points the page
at it ("use a file I already have"). The model is kept outside the tool folder, in
~/.app-builder/models, so ZIP updates never copy or lose several GB.

Nothing here is needed for builds: when AI is off or broken, every caller falls back to rules.
"""
from __future__ import annotations
import hashlib
import json
import os
import re
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from .config import DATA, ROOT
from .telemetry import safe_text

SETTINGS_FILE = DATA / 'ai_settings.json'
MODEL_DIR = Path(os.environ.get('APP_BUILDER_MODEL_DIR', Path.home() / '.app-builder' / 'models'))
RUNTIME = 'llama-cpp-python>=0.3.8,<1'          # 0.3.8+ knows Phi-4-mini and Qwen3
HF = 'https://huggingface.co'
# Models are picked for: short English output, small enough for a 16 GB Mac, permissive licence.
MODELS = {
    'phi4-mini': {'label': 'Phi-4-mini-instruct · Q4_K_M · 约 2.5 GB', 'licence': 'MIT（Microsoft）',
                  'repo': 'bartowski/microsoft_Phi-4-mini-instruct-GGUF', 'suffix': 'Q4_K_M.gguf',
                  'source': 'https://huggingface.co/microsoft/Phi-4-mini-instruct'},
    'qwen3-4b': {'label': 'Qwen3-4B · Q4_K_M · 约 2.5 GB', 'licence': 'Apache 2.0（阿里通义，官方 GGUF）',
                 'repo': 'Qwen/Qwen3-4B-GGUF', 'suffix': 'Q4_K_M.gguf',
                 'source': 'https://huggingface.co/Qwen/Qwen3-4B'},
}
DEFAULT_MODEL = 'phi4-mini'
FEATURES = ('tester_note', 'failure_hint')
_JOB_LOCK = threading.Lock()
_JOB = {'running': False, 'step': '', 'progress': None, 'log': [], 'error': '', 'finished': ''}


# ---- settings -----------------------------------------------------------------------------
def saved():
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for key in ('enabled', *FEATURES):
        if isinstance(data.get(key), bool):
            out[key] = data[key]
    if isinstance(data.get('model_path'), str):
        out['model_path'] = data['model_path']
    if data.get('model') in MODELS or data.get('model') == 'custom':
        out['model'] = data['model']
    return out


def save(values):
    data = {**saved(), **values}
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, SETTINGS_FILE)
    return data


def effective(config):
    """config['ai'] with the page's settings on top (the page wins)."""
    ai = dict((config or {}).get('ai') or {})
    page = saved()
    for key in ('enabled', 'model_path', *FEATURES):
        if key in page:
            ai[key] = page[key]
    ai.setdefault('tester_note', True)
    ai.setdefault('failure_hint', True)
    ai['model'] = page.get('model', '')
    return ai


def feature_on(config, name):
    ai = effective(config)
    return bool(ai.get('enabled') and ai.get('model_path') and ai.get(name))


# ---- checks -------------------------------------------------------------------------------
def runtime_version():
    """Version of llama-cpp-python in this interpreter, or '' (checked in a child process so
    a broken native library never takes the web server down)."""
    try:
        out = subprocess.run([sys.executable, '-c', 'import llama_cpp;print(llama_cpp.__version__)'],
                             capture_output=True, text=True, timeout=60)
    except Exception:
        return ''
    return out.stdout.strip() if out.returncode == 0 else ''


def check_model_file(path):
    """A readable GGUF file of plausible size, else ValueError (Chinese, for the page)."""
    p = Path(str(path or '')).expanduser()
    if not p.is_file():
        raise ValueError('找不到这个文件：' + str(p))
    if p.suffix.lower() != '.gguf':
        raise ValueError('模型文件应以 .gguf 结尾')
    if p.stat().st_size < 300 * 1024 * 1024:
        raise ValueError('文件太小，不像完整的模型（可能没下载完）')
    with p.open('rb') as f:
        if f.read(4) != b'GGUF':
            raise ValueError('不是 GGUF 格式的模型文件')
    return p


def status():
    page = saved()
    path = page.get('model_path') or ''
    model_ok, model_note = False, ''
    if path:
        try:
            check_model_file(path)
            model_ok = True
        except ValueError as exc:
            model_note = str(exc)
    with _JOB_LOCK:
        job = {k: (list(v) if isinstance(v, list) else v) for k, v in _JOB.items()}
    return {'enabled': bool(page.get('enabled')), 'model': page.get('model', ''), 'model_path': path,
            'model_ok': model_ok, 'model_note': model_note, 'runtime': runtime_version(),
            'features': {k: page.get(k, True) for k in FEATURES},
            'models': {k: {'label': v['label'], 'licence': v['licence'], 'source': v['source']} for k, v in MODELS.items()},
            'model_dir': str(MODEL_DIR), 'job': job}


# ---- install job --------------------------------------------------------------------------
def _say(step=None, line=None, progress=None):
    with _JOB_LOCK:
        if step is not None:
            _JOB['step'] = step
        if line:
            _JOB['log'] = (_JOB['log'] + [f'{datetime.now():%H:%M:%S} {safe_text(line)}'])[-60:]
        _JOB['progress'] = progress


def _ssl():
    cafile = os.environ.get('SSL_CERT_FILE') or os.environ.get('REQUESTS_CA_BUNDLE')
    if cafile:
        return ssl.create_default_context(cafile=cafile)
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _open(url, headers=None, timeout=60):
    req = urllib.request.Request(url, headers={'User-Agent': 'app-builder', **(headers or {})})
    return urllib.request.urlopen(req, timeout=timeout, context=_ssl())


def install_runtime():
    if runtime_version():
        _say(line='运行库已安装：llama-cpp-python ' + runtime_version())
        return
    _say('检查 pip 来源', '使用公司现有 pip 配置（与 install.sh 相同的检查）')
    check = subprocess.run([sys.executable, '-m', 'appbuilder.pip_sources'], cwd=str(ROOT),
                           capture_output=True, text=True, timeout=120)
    if check.returncode != 0:
        raise RuntimeError('pip 来源检查未通过：' + (check.stderr or check.stdout).strip()[-400:])
    _say('安装运行库', f'pip install {RUNTIME}（首次需要本机编译，约 3–10 分钟）')
    env = {**os.environ, 'CMAKE_ARGS': os.environ.get('CMAKE_ARGS', '-DGGML_METAL=on')}
    proc = subprocess.Popen([sys.executable, '-m', 'pip', '--disable-pip-version-check', 'install', RUNTIME],
                            cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    tail = []
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            tail = (tail + [line])[-20:]
            if re.match(r'(Collecting|Building|Successfully|Installing|ERROR)', line):
                _say(line=line[:200])
    if proc.wait() != 0:
        hint = ''
        if any('xcrun' in t or 'CMAKE_C_COMPILER' in t or 'clang' in t for t in tail):
            hint = '（需要 Xcode Command Line Tools：终端运行 xcode-select --install）'
        raise RuntimeError('运行库安装失败' + hint + '：' + ' | '.join(tail[-4:])[-500:])
    if not runtime_version():
        raise RuntimeError('运行库装好了但无法加载，请把上面的日志发给维护者')
    _say(line='运行库安装完成：llama-cpp-python ' + runtime_version())


def model_listing(key):
    """(file name, size, sha256) Hugging Face publishes for the chosen model."""
    spec = MODELS[key]
    with _open(f'{HF}/api/models/{spec["repo"]}/tree/main') as r:
        files = json.loads(r.read())
    for f in files if isinstance(files, list) else []:
        if str(f.get('path', '')).endswith(spec['suffix']) and '/' not in f['path']:
            lfs = f.get('lfs') or {}
            return f['path'], int(lfs.get('size') or f.get('size') or 0), str(lfs.get('oid') or '')
    raise RuntimeError('在 Hugging Face 上没找到模型文件 ' + spec['suffix'])


def _sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(8 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def download_model(key):
    spec = MODELS[key]
    _say('读取模型信息', f'Hugging Face：{spec["repo"]}')
    name, size, sha = model_listing(key)
    if not re.fullmatch(r'[0-9a-f]{64}', sha):
        raise RuntimeError('Hugging Face 没有给出这个文件的校验值，为安全起见不下载')
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    final = MODEL_DIR / name
    if final.is_file() and final.stat().st_size == size:
        _say('校验模型', '已存在同名文件，核对校验值')
        if _sha256(final) == sha:
            return final
        final.unlink()
    part = final.with_suffix(final.suffix + '.part')
    have = part.stat().st_size if part.exists() else 0
    url = f'{HF}/{spec["repo"]}/resolve/main/{name}'
    _say('下载模型', f'{name}（{size / 1e9:.1f} GB）' + ('，接着上次继续' if have else ''))
    headers = {'Range': f'bytes={have}-'} if have else {}
    with _open(url, headers, timeout=120) as r, part.open('ab' if have and r.status == 206 else 'wb') as out:
        if r.status != 206:
            have = 0
        last = 0.0
        while True:
            block = r.read(4 * 1024 * 1024)
            if not block:
                break
            out.write(block)
            have += len(block)
            if time.monotonic() - last > 1:
                last = time.monotonic()
                _say(progress=round(have * 100 / size, 1) if size else None)
    if size and part.stat().st_size != size:
        raise RuntimeError('下载不完整，再点一次会接着下载')
    _say('校验模型', '核对 SHA-256')
    if _sha256(part) != sha:
        part.unlink()
        raise RuntimeError('模型文件校验不一致，已删除，请重试')
    os.replace(part, final)
    _say(line=f'模型已保存：{final}')
    return final


def self_test(path):
    """Load the model in a child process and ask for a tiny JSON answer."""
    _say('试运行', '加载模型并试答一句（首次约 10–30 秒）')
    code = ('import json,sys;from appbuilder.local_llm import LocalLLM;'
            'm=LocalLLM(sys.argv[1],max_tokens=32);'
            'r=m.ask_json("Reply with JSON only.","Return {\\"ok\\": true}");'
            'print(json.dumps(r))')
    out = subprocess.run([sys.executable, '-c', code, str(path)], cwd=str(ROOT),
                         capture_output=True, text=True, timeout=300)
    try:
        ok = json.loads(out.stdout.strip().splitlines()[-1]).get('ok') is True
    except Exception:
        ok = False
    if not ok:
        raise RuntimeError('模型试运行没有得到正确回答：' + (out.stderr or out.stdout).strip()[-300:])
    _say(line='试运行通过')


def _run(kind, value):
    try:
        install_runtime()
        if kind == 'download':
            path = download_model(value)
            model = value
        else:
            path = check_model_file(value)
            model = 'custom'
        self_test(path)
        save({'enabled': True, 'model_path': str(path), 'model': model})
        from . import local_llm
        local_llm._CACHE.clear()
        _say('完成', '离线 AI 已启用')
        with _JOB_LOCK:
            _JOB['finished'] = 'ok'
    except Exception as exc:
        with _JOB_LOCK:
            _JOB['error'] = safe_text(str(exc))[:800]
            _JOB['finished'] = 'failed'
        _say(line='失败：' + safe_text(str(exc))[:300])
    finally:
        with _JOB_LOCK:
            _JOB['running'] = False


def start(kind, value):
    """kind='download' with a MODELS key, or kind='file' with a local path."""
    if kind == 'download' and value not in MODELS:
        raise ValueError('未知的模型')
    if kind == 'file':
        check_model_file(value)
    elif kind != 'download':
        raise ValueError('未知的安装方式')
    with _JOB_LOCK:
        if _JOB['running']:
            raise ValueError('正在安装中，请等它完成')
        _JOB.update(running=True, step='开始', progress=None, log=[], error='', finished='')
    threading.Thread(target=_run, args=(kind, value), daemon=True, name='ai-setup').start()
    return status()
