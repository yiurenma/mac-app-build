"""Install the bundled VS Code extension by copying it into VS Code's user extensions folder."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from . import copilot_bridge

SOURCE = copilot_bridge.EXTENSION_DIR
SKIP = shutil.ignore_patterns('node_modules', 'test', '*.vsix', '.vscode-test')


def meta():
    return json.loads((SOURCE / 'package.json').read_text(encoding='utf-8'))


def extensions_dir():
    return Path(os.environ.get('APP_BUILDER_VSCODE_EXTENSIONS') or Path.home() / '.vscode' / 'extensions')


def target():
    m = meta()
    return extensions_dir() / f"{m['publisher']}.{m['name']}-{m['version']}"


def _installed():
    m = meta()
    return sorted(extensions_dir().glob(f"{m['publisher']}.{m['name']}-*"))


def install():
    if not (SOURCE / 'extension.js').is_file():
        raise RuntimeError('找不到 vscode-extension 文件夹')
    root = extensions_dir()
    if not root.parent.exists():
        raise RuntimeError(f'没找到 VS Code 的用户目录 {root.parent}：先安装并打开一次 VS Code')
    for old in _installed():
        shutil.rmtree(old)
    dest = target()
    shutil.copytree(SOURCE, dest, ignore=SKIP)
    return dest


def uninstall():
    found = _installed()
    for old in found:
        shutil.rmtree(old)
    return found


def run(action):
    if action == 'install':
        dest = install()
        return f'已安装到 {dest}\n请完全退出并重新打开 VS Code，登录 Copilot；第一次调用时点“允许”。然后在设置页选择“VS Code 里的 Copilot”。'
    if action == 'uninstall':
        return '已卸载（重启 VS Code 后生效）' if uninstall() else '没有安装'
    state = '已安装' if _installed() else '未安装'
    info = copilot_bridge.check()
    return f"插件文件：{state}\n运行状态：{info['note']}"
