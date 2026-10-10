#!/bin/bash
# Double-click on macOS: safe update (Git checkout only), install if needed,
# run doctor, start localhost Web Console and open the default browser.
set -e
cd "$(dirname "$0")"
echo '=========================================='
echo ' Mac APP Builder · v0.6 Web Console'
echo '=========================================='
if git rev-parse --show-toplevel >/dev/null 2>&1 && [ -d .git ]; then
  echo '[UPDATE] 检查 GitHub 最新版本（仅允许干净工作区安全快进）'
  ./app-build web --update "$@"
else
  # A ZIP distribution is usable without Git metadata.
  ./app-build web "$@"
fi
