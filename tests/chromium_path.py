"""Where the tests' Chromium lives: /usr/bin/chromium if present (CI/sandbox link), else
Playwright's own download (macOS, Windows). Resolved in a subprocess so importing this
never leaves a Playwright event loop running inside the test process."""
import os
import subprocess
import sys


def _find():
    fixed = '/usr/bin/chromium'
    if os.path.exists(fixed):
        return fixed
    code = ("from playwright.sync_api import sync_playwright\n"
            "p = sync_playwright().start(); print(p.chromium.executable_path); p.stop()")
    try:
        out = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=60).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return fixed
    return out.splitlines()[-1] if out else fixed


CHROMIUM = _find()
