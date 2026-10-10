"""Idempotent macOS desktop entry: reuse an existing local Web Console.

Stdlib-only so this check works before installing Playwright or PyYAML.
Exit 0: existing Web Console opened; 1: port free (start a new one);
2: port in use by something else / older incompatible version.
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import webbrowser
import http.client


def probe(port: int) -> str:
    """Return 'running', 'free', or 'occupied'. Never follow remote redirects."""
    if not (1024 <= port <= 65535):
        raise ValueError('端口必须在 1024–65535 之间')
    try:
        # Check the port without depending on HTTP success. A 404 from v0.6
        # or an unrelated service is still an occupied port.
        with socket.create_connection(('127.0.0.1', port), timeout=0.7):
            pass
    except ConnectionRefusedError:
        return 'free'
    except OSError as exc:
        # Unknown errors should not be mistaken for an available port.
        if getattr(exc, 'errno', None) in (61, 111):
            return 'free'
        return 'occupied'
    # Direct local HTTP connection; no proxies and no redirect-following.
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=1.5)
    try:
        conn.request('GET', '/api/health', headers={'Host': f'127.0.0.1:{port}'})
        response = conn.getresponse()
        if response.status == 200:
            data = json.loads(response.read(2048))
            if isinstance(data, dict) and data.get('app') == 'mac-app-builder' and data.get('service') == 'web-console':
                return 'running'
    except (OSError, TimeoutError, ValueError, json.JSONDecodeError):
        pass
    finally:
        conn.close()
    return 'occupied'


def main(argv=None):
    parser = argparse.ArgumentParser(description='复用已经打开的 Mac APP Builder Web Console')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--no-open', action='store_true')
    args = parser.parse_args(argv)
    try:
        status = probe(args.port)
    except ValueError as exc:
        parser.error(str(exc))
    url = f'http://127.0.0.1:{args.port}'
    if status == 'running':
        print(f'[WEB] Console 已经运行，复用现有页面：{url}', flush=True)
        if not args.no_open:
            webbrowser.open(url)
        return 0
    if status == 'occupied':
        print(f'[WEB] 端口 {args.port} 已被其他程序或旧版 Console 占用。'
              f'请先关闭原服务，或使用 --port 指定另一端口。', file=sys.stderr)
        return 2
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
