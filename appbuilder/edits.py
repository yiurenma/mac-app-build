from __future__ import annotations
import json
import re
from pathlib import Path
from .versions import ARTIFACTS

XCCONFIGS = (
    "PayByBankDomainModelPluginKit/ProjectConfigurations/PayByBankDomainModelPluginKit.xcconfig",
    "ProjectConfigurations/PayByBankJourneyPluginKit.xcconfig",
)
GROUP = 'paybybank-ios-plugin'


def edit_plugin_versions(root: Path, version: str):
    """Two targeted assignment edits. No reformatting, newline conversion or unrelated changes."""
    changes = []
    pending = []
    pattern = re.compile(r'(?m)^([ \t]*BUNDLE_VERSION_SHORT[ \t]*=[ \t]*)([^\s#;]+)([^\r\n]*)(\r?)$')
    for rel in XCCONFIGS:
        f = root / rel
        old = f.read_bytes()
        src = old.decode('utf-8')
        matches = list(pattern.finditer(src))
        if len(matches) != 1:
            raise ValueError(f"必须恰有一个 BUNDLE_VERSION_SHORT: {rel} (实际 {len(matches)})")
        target = pattern.sub(lambda m: m[1] + version + m[3] + m[4], src)
        pending.append((f, target.encode('utf-8')))
        changes.append((rel, matches[0][2], version))
    # Write only after every file is validated.
    for f, body in pending:
        f.write_bytes(body)
    return changes


def leaf_objects(text):
    """Enumerate JSON object spans, ignoring delimiters inside quoted strings."""
    stack, in_str, escaped = [], False, False
    for i, ch in enumerate(text):
        if in_str:
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == '{':
            stack.append((i, False))
        elif ch == '}':
            if not stack:
                raise ValueError('JSON braces not balanced')
            start, has_child = stack.pop()
            if not has_child:
                yield start, i + 1
            if stack:
                parent_start, _ = stack[-1]
                stack[-1] = (parent_start, True)


def matching_entries(text):
    json.loads(text)  # strict validation
    found = {}
    for start, end in leaf_objects(text):
        segment = text[start:end]
        try:
            obj = json.loads(segment)
        except json.JSONDecodeError:
            continue
        if (isinstance(obj, dict) and obj.get('group') == GROUP
                and obj.get('library') in ARTIFACTS):
            found.setdefault(obj['library'], []).append((start, end, obj))
    for name in ARTIFACTS:
        if len(found.get(name, [])) != 1:
            raise ValueError(f"{name} 在 JSON 中必须恰好出现一次，实际 {len(found.get(name, []))}")
    return found


def update_mobile_ci(path: Path, version: str):
    original = path.read_bytes()
    text = original.decode('utf-8')
    matches = matching_entries(text)
    replacements = []
    for name in ARTIFACTS:
        start, end, obj = matches[name][0]
        if not isinstance(obj.get('version'), str):
            raise ValueError(f"{name} 没有 string 类型的 version")
        segment = text[start:end]
        pattern = re.compile(r'("version"[ \t\r\n]*:[ \t\r\n]*")([^"\\]*(?:\\.[^"\\]*)*)(")')
        found = list(pattern.finditer(segment))
        if len(found) != 1:
            raise ValueError(f"{name}: 无法定位唯一 version 属性")
        m = found[0]
        replacements.append((start + m.start(2), start + m.end(2), version))
    # preserve 100% of original whitespace / key ordering
    for a, b, val in sorted(replacements, reverse=True):
        text = text[:a] + val + text[b:]
    if len(text) < 1:
        raise ValueError('empty JSON')
    parsed = matching_entries(text)
    for name in ARTIFACTS:
        if parsed[name][0][2]['version'] != version:
            raise ValueError(f'未能修改 {name}')
    path.write_bytes(text.encode('utf-8'))
    return {name: matches[name][0][2]['version'] for name in ARTIFACTS}


def validate_lock(path: Path, version: str):
    content = path.read_text(encoding='utf-8')
    entries = matching_entries(content)
    digest = {}
    for name in ARTIFACTS:
        obj = entries[name][0][2]
        if obj.get('version') != version:
            raise ValueError(f"{name} lock version={obj.get('version')} != {version}")
        sha = obj.get('sha256', '')
        if not isinstance(sha, str) or not re.fullmatch(r'[a-fA-F0-9]{64}', sha):
            raise ValueError(f'{name} lock SHA256 无效：必须为 64 位十六进制')
        digest[name] = sha
    return digest


def ensure_lock_other_entries_unchanged(before: str, after: str):
    """Allow dependency resolver to change only target version/hash, not other packages."""
    def normalized(text):
        data = json.loads(text)
        def visit(obj):
            if isinstance(obj, list):
                return [visit(e) for e in obj]
            if isinstance(obj, dict):
                d = {k: visit(v) for k, v in obj.items()}
                if d.get('group') == GROUP and d.get('library') in ARTIFACTS:
                    d['version'] = '__target__'
                    d['sha256'] = '__target__'
                return d
            return obj
        return visit(data)
    if normalized(before) != normalized(after):
        raise ValueError('bootstrap.sh 改动了 mobile-ci.lock 中其他依赖，请人工检查')
