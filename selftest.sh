#!/bin/sh
# Run the offline, non-network, non-mutating quality gate three times.
set -eu
cd "$(dirname "$0")"
if [ -x .venv/bin/python ]; then
  PY=.venv/bin/python
else
  PY=python3
fi
mkdir -p docs/verification-logs
for i in 1 2 3; do
  log="docs/verification-logs/round${i}.txt"
  echo "[$i/3] 需求→代码对照后的完整离线回归，日志：$log"
  (
    echo "=== FINAL VERIFICATION ROUND $i ==="
    echo '$ python -m unittest discover -s tests -v'
    "$PY" -m unittest discover -s tests -v || exit 1
    echo "exit=0"
    echo '$ python -m compileall -q appbuilder'
    "$PY" -m compileall -q appbuilder || exit 1
    echo "exit=0"
    echo '$ sh -n install.sh app-build selftest.sh'
    sh -n install.sh app-build selftest.sh || exit 1
    echo "exit=0"
    echo '$ python -m appbuilder run --plugin-version 1.3 --plugin-branch develop --gmx-branch feature/UKPBB-203745-test --country uk --project-name PayByBank --dry-run'
    "$PY" -m appbuilder run --plugin-version 1.3 --plugin-branch develop --gmx-branch feature/UKPBB-203745-test --country uk --project-name PayByBank --dry-run || exit 1
    echo "exit=0"
  ) > "$log" 2>&1 || { echo "第 $i 轮失败，请查看 $log" >&2; tail -n 100 "$log" >&2; exit 1; }
  # Keep checked-in evidence whitespace-clean despite unittest's progress output.
  "$PY" -c 'import pathlib,sys; p=pathlib.Path(sys.argv[1]); p.write_text("\n".join(line.rstrip() for line in p.read_text().splitlines())+"\n")' "$log"
  grep -E 'Ran [0-9]+ tests|^OK|\.\.\. skipped ' "$log" || true
done
echo '三轮离线回归完成。真正 Nexus/SSO/Jenkins/上传仍须在 Mac 内网联调。'
