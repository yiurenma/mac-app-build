"""Everything about one pull request, as plain text for the AI (only for models with room for it).

Used for the optional tester note: description, commit messages, the changed files and a trimmed
diff. Every GitHub failure just returns what was gathered so far - it never blocks a build.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

MAX_TOTAL = 30000          # characters of context sent in addition to title and description
MAX_PATCH_PER_FILE = 1500
MAX_PATCH_TOTAL = 18000
MAX_FILES = 100
MAX_COMMITS = 100
SKIP_PATCH = re.compile(r'(^|/)(Pods|Carthage|node_modules|vendor)/|\.(lock|resolved|pbxproj|xcworkspacedata|png|jpe?g|pdf|svg|xcassets|plist)$|Package\.resolved$', re.I)


def parse_pr_url(url):
    """https://host/org/repo/pull/12 -> (repo_url, number)."""
    parts = urlsplit((url or '').strip())
    bits = parts.path.strip('/').split('/')
    if parts.scheme != 'https' or len(bits) < 4 or bits[2] != 'pull' or not bits[3].isdigit():
        raise ValueError('请粘贴 PR 链接，例如 https://主机/组织/仓库/pull/123')
    return f'https://{parts.netloc}/{bits[0]}/{bits[1]}', int(bits[3])


def fetch(repo_get, number, *, max_total=MAX_TOTAL):
    """Return (text, stats). repo_get(path) -> parsed JSON; may raise."""
    stats = {'commits': 0, 'files': 0, 'patched': 0}
    lines = []
    try:
        commits = repo_get(f'/pulls/{number}/commits?per_page={MAX_COMMITS}') or []
    except Exception:  # noqa: BLE001
        commits = []
    if commits:
        lines.append('Commits in this pull request:')
        for c in commits[:MAX_COMMITS]:
            message = ((c.get('commit') or {}).get('message') or '').strip()
            head, _, rest = message.partition('\n')
            rest = ' '.join(rest.split())[:300]
            lines.append('- ' + head[:200] + (f' ({rest})' if rest else ''))
            stats['commits'] += 1
    try:
        files = repo_get(f'/pulls/{number}/files?per_page={MAX_FILES}') or []
    except Exception:  # noqa: BLE001
        files = []
    if files:
        lines.append('')
        lines.append('Changed files:')
        for f in files[:MAX_FILES]:
            lines.append(f"- {f.get('filename', '?')} ({f.get('status', 'modified')}, +{f.get('additions', 0)} -{f.get('deletions', 0)})")
            stats['files'] += 1
        lines.append('')
        lines.append('Code changes (trimmed):')
        budget = MAX_PATCH_TOTAL
        for f in files[:MAX_FILES]:
            patch = f.get('patch') or ''
            name = f.get('filename', '')
            if not patch or SKIP_PATCH.search(name) or budget <= 0:
                continue
            piece = patch[:MAX_PATCH_PER_FILE] + ('\n... (cut)' if len(patch) > MAX_PATCH_PER_FILE else '')
            piece = piece[:budget]
            budget -= len(piece)
            lines.append(f'--- {name}')
            lines.append(piece)
            stats['patched'] += 1
    text = '\n'.join(lines)
    if len(text) > max_total:
        text = text[:max_total] + '\n... (cut)'
    return text, stats
