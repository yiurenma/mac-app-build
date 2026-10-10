#!/bin/bash
# Double-click macOS launcher: reuse a running Console, or safely start one.
set -e
cd "$(dirname "$0")"
echo '=========================================='
echo ' Mac APP Builder · v0.6.2 Web Console'
echo '=========================================='
# Reuse an already running server instead of failing with "Address already in use".
# This module only uses Python's standard library (no install needed).
if python3 -m appbuilder.launcher "$@"; then
  exit 0
else
  rc=$?
  if [ "$rc" -ne 1 ]; then exit "$rc"; fi
fi
if git rev-parse --show-toplevel >/dev/null 2>&1 && [ -d .git ]; then
  echo '[UPDATE] 安全检查新版本；离线或本地修改时沿用当前版本继续启动。'
  exec ./app-build web --update "$@"
fi
exec ./app-build web "$@"
