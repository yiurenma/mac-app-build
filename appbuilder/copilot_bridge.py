"""Ask VS Code's Copilot through the small local extension in vscode-extension/.

Same surface as LocalLLM (``available()`` / ``ask_json()``): any problem returns None and the
caller falls back to plain rules. Only 127.0.0.1 is contacted; the token comes from a 0600 file
the extension writes, never from config or the page.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from .telemetry import action, safe_text

INFO = Path(os.environ.get('APP_BUILDER_BRIDGE_INFO') or Path.home() / '.app-builder' / 'vscode-bridge.json')
TIMEOUT = 100
START_WAIT = float(os.environ.get('APP_BUILDER_VSCODE_WAIT') or 60)    # seconds to wait for a cold start
RETRY_AFTER = 300                                                     # do not relaunch more often than this after a failure
_START_LOCK = threading.Lock()
_LAST_FAILED = [0.0]
EXTENSION_DIR = Path(__file__).resolve().parent.parent / 'vscode-extension'


def read_info(path=None):
    """(port, token) from the extension's info file, or None."""
    try:
        data = json.loads(Path(path or INFO).read_text(encoding='utf-8'))
        port, token = int(data['port']), str(data['token'])
        pid = int(data.get('pid') or 0)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if pid:
        try:
            os.kill(pid, 0)                  # a crashed VS Code leaves its file behind
        except ProcessLookupError:
            return None
        except OSError:
            pass
    return (port, token) if 0 < port < 65536 and token else None


def _call(method, route, payload=None, *, timeout, info=None):
    got = read_info(info)
    if not got:
        raise ConnectionError('VS Code 插件没有运行（找不到 vscode-bridge.json）')
    port, token = got
    data = None if payload is None else json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(f'http://127.0.0.1:{port}{route}', data=data, method=method,
                                 headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))      # never via a proxy
    try:
        with opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read().decode('utf-8')).get('error', '')
        except (ValueError, OSError):
            message = ''
        raise RuntimeError(message or f'HTTP {exc.code}') from None
    except (urllib.error.URLError, OSError) as exc:
        raise ConnectionError('连不上 VS Code 插件（VS Code 可能已关闭）') from exc


def check(info=None):
    """{'ok': bool, 'note': str} for the settings page."""
    if not read_info(info):
        return {'ok': False, 'note': '没有检测到 VS Code 插件：先安装插件并打开 VS Code'}
    try:
        _call('GET', '/health', timeout=5, info=info)
    except (ConnectionError, RuntimeError) as exc:
        return {'ok': False, 'note': str(exc)}
    return {'ok': True, 'note': '插件在运行'}


def _launch_command():
    """Command that opens VS Code in the background, or None. APP_BUILDER_VSCODE_LAUNCH overrides."""
    custom = os.environ.get('APP_BUILDER_VSCODE_LAUNCH')
    if custom:
        import shlex
        return shlex.split(custom)
    if sys.platform == 'darwin':
        for name in ('Visual Studio Code', 'Visual Studio Code - Insiders'):
            if any(os.path.isdir(os.path.join(base, name + '.app'))
                   for base in ('/Applications', os.path.expanduser('~/Applications'))):
                return ['open', '-g', '-j', '-a', name]      # background and hidden: no focus stealing
        return None
    code = shutil.which('code')
    return [code] if code else None


def ensure_running(wait=None, *, sleep=time.sleep, now=time.monotonic):
    """True when the extension answers. Opens VS Code in the background if it is not running,
    then waits for the extension. After a failed attempt it does not try again for a while,
    so one closed VS Code cannot slow down every ticket."""
    if check()['ok']:
        return True
    with _START_LOCK:
        if check()['ok']:
            return True
        if _LAST_FAILED[0] and now() - _LAST_FAILED[0] < RETRY_AFTER:
            return False
        command = _launch_command()
        if not command:
            _LAST_FAILED[0] = now()
            return False
        action('AI', '正在后台打开 VS Code，等待 Copilot 插件就绪…')
        try:
            subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        except OSError as exc:
            action('AI', safe_text(f'无法打开 VS Code: {exc}; 使用纯规则'))
            _LAST_FAILED[0] = now()
            return False
        deadline = now() + (START_WAIT if wait is None else wait)
        while now() < deadline:
            if check()['ok']:
                _LAST_FAILED[0] = 0.0
                return True
            sleep(1)
        action('AI', 'VS Code 没有在时限内就绪（可能还没登录 Copilot）；使用纯规则')
        _LAST_FAILED[0] = now()
        return False


class CopilotLLM:
    model_path = None
    rich = True            # big context: can be given the whole pull request

    def __init__(self, info=None, timeout=TIMEOUT, auto_start=False):
        self.info, self.timeout, self.auto_start = info, timeout, auto_start

    def available(self):
        return check(self.info)['ok']

    def _ready(self):
        if check(self.info)['ok']:
            return True
        return bool(self.auto_start and self.info is None and ensure_running())

    def ask_json(self, system, user):
        """Parsed JSON object or None. The model is asked for JSON in the prompt itself."""
        try:
            if not self._ready():
                raise ConnectionError('VS Code 插件没有响应')
            out = _call('POST', '/ask', {'system': system, 'user': user}, timeout=self.timeout, info=self.info)
            text = str(out.get('text') or '')
            match = re.search(r'\{.*\}', text, re.S)
            data = json.loads(match.group(0) if match else text)
            return data if isinstance(data, dict) else None
        except Exception as exc:  # noqa: BLE001 - never block a build or an email
            action('AI', safe_text(f'VS Code Copilot 调用失败: {type(exc).__name__} {exc}; 使用纯规则'))
            return None
