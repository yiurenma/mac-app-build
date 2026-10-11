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
    echo '$ complete unittest discovery with strict zero-skip accounting'
    "$PY" - <<'PY' || exit 1
import json
import re
import sys
import unittest

class CountedResult(unittest.TextTestResult):
    passed = 0
    def addSuccess(self, test):
        self.passed += 1
        super().addSuccess(test)

def test_ids(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from test_ids(test)
        else:
            yield test.id()

suite = unittest.defaultTestLoader.discover('tests')
ids = list(test_ids(suite))
result = unittest.TextTestRunner(verbosity=2, resultclass=CountedResult).run(suite)
skipped = 0
for test, reason in result.skipped:
    group = re.fullmatch(r'setUp(Class|Module) \((.+)\)', test.id())
    skipped += sum(name.startswith(group[2] + '.') for name in ids) if group else 1
counts = dict(discovered=len(ids), run=result.testsRun, passed=result.passed,
              failures=len(result.failures), errors=len(result.errors), skipped=skipped,
              skip_events=len(result.skipped), unexecuted=len(ids)-result.testsRun,
              expected_failures=len(result.expectedFailures),
              unexpected_successes=len(result.unexpectedSuccesses))
print('SUITE_COUNTS=' + json.dumps(counts), flush=True)
complete = (len(ids) > 0 and len(ids) == result.testsRun == result.passed
            and result.wasSuccessful() and not result.skipped
            and not result.expectedFailures and not result.unexpectedSuccesses)
sys.exit(0 if complete else 1)
PY
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
  grep -E 'Ran [0-9]+ tests|^OK|\.\.\. skipped |^SUITE_COUNTS=' "$log" || true
done
echo '三轮离线回归完成。真正 Nexus/SSO/Jenkins/上传仍须在 Mac 内网联调。'
