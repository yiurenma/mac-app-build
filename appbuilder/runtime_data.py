"""Bounded, read-only state snapshots. Never return the unsanitized state."""
import json
import os
import re
import stat
from .telemetry import safe_text

MAX_BYTES = 1_048_576
ID = re.compile(r'[0-9]{12}-[0-9a-f]{6}\Z')
SECRET = re.compile(r'password|passwd|secret|token|cookie|authorization|credential|private.?key|api.?key|access.?key|signature|recipient|email|account|username|session|^auth$|^user$|^headers$|^key$|mail_preview|body|subject|^to$|^from$', re.I)
EMAIL = re.compile(r'[A-Za-z0-9.!#$%&\'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')

def sanitize(value, depth=0, budget=None):
    budget = [10000] if budget is None else budget
    budget[0] -= 1
    if depth > 20 or budget[0] < 0:
        raise ValueError('运行数据层级或字段数量超出限制')
    if isinstance(value, dict):
        return {sanitize(str(k), depth+1, budget): '[已隐藏]' if SECRET.search(str(k)) else sanitize(v, depth+1, budget) for k,v in value.items()}
    if isinstance(value, list):
        return [sanitize(v, depth+1, budget) for v in value]
    if isinstance(value, str):
        text = re.sub(r'([A-Za-z][A-Za-z0-9+.-]*://)[^\s/@]+@', r'\1', value)
        text = re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----', '[密钥已隐藏]', text, flags=re.S)
        text = EMAIL.sub('[邮箱已隐藏]', safe_text(text))
        return re.sub(r'(https?://[^\s/]+/join/)[^\s"<>]+', r'\1[已隐藏]', text)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise ValueError('运行数据包含不支持的格式')

def snapshot(task_id, data_root, lookup):
    if not isinstance(task_id, str) or not ID.fullmatch(task_id):
        raise ValueError('任务编号无效')
    row = lookup(task_id)
    if not row:
        raise ValueError('任务不存在')
    descriptors = []
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        root = os.open(data_root, flags); descriptors.append(root)
        runs = os.open('runs', flags, dir_fd=root); descriptors.append(runs)
        folder = os.open(task_id, flags, dir_fd=runs); descriptors.append(folder)
        fd = os.open('state.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder); descriptors.append(fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise ValueError('运行数据不是普通文件或超过 1 MB')
        with os.fdopen(os.dup(fd), 'rb') as stream:
            raw = stream.read(MAX_BYTES+1)
        if len(raw) > MAX_BYTES:
            raise ValueError('运行数据超过 1 MB')
        state = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('非法数值')))
        if not isinstance(state, dict) or not isinstance(state.get('completed'), list) or not isinstance(state.get('runtime'), dict):
            raise ValueError('运行数据格式无效')
        return {'task':sanitize({k:row.get(k) for k in ('id','status','stage','created')}), 'state':sanitize(state)}
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise ValueError('运行数据缺失、损坏或文件路径不安全') from None
    finally:
        for fd in reversed(descriptors):
            os.close(fd)
