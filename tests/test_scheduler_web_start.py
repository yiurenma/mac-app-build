"""Offline startup tests; subprocesses use a copied package with an empty private DB."""
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from appbuilder import scheduler, web


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        p = patch.object(scheduler, 'DATA', self.data)
        p.start()
        self.addCleanup(p.stop)

    def test_stale_pid_and_config_conflict_never_kill_or_duplicate(self):
        scheduler.write_heartbeat({'pid': 99999999})
        with scheduler.lock_path().open('a+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertFalse(scheduler.heartbeat_status()['online'])
            scheduler.write_heartbeat({'config': '/different/config.yaml'})
            with patch.object(scheduler.subprocess, 'Popen') as spawn:
                scheduler.SchedulerSupervisor().ensure_running()
                spawn.assert_not_called()
            self.assertIn('不同配置', scheduler.heartbeat_status()['reason'])

    def test_reuse_healthy_and_report_spawn_failure(self):
        with scheduler.lock_path().open('a+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            scheduler.write_heartbeat({'config': scheduler.config_path()})
            with patch.object(scheduler.subprocess, 'Popen') as spawn:
                scheduler.SchedulerSupervisor().ensure_running()
                spawn.assert_not_called()
        with patch.object(scheduler.subprocess, 'Popen', side_effect=OSError('spawn denied')):
            monitor = scheduler.SchedulerSupervisor()
            monitor.start()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and 'spawn denied' not in scheduler.heartbeat_status().get('startup', ''):
                time.sleep(.02)
            monitor.stop()
            self.assertIn('spawn denied', scheduler.heartbeat_status()['startup'])
            self.assertFalse(scheduler.heartbeat_status()['online'])

    def test_port_collision_does_not_start_monitor(self):
        with patch.object(scheduler.SchedulerSupervisor, 'start') as start:
            with self.assertRaises(OSError):
                web.serve(open_browser=False, server_factory=lambda *a: (_ for _ in ()).throw(OSError('port busy')))
            start.assert_not_called()


class ProcessLifecycleTests(unittest.TestCase):
    def test_two_webs_share_scheduler_and_survive_web_close(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(Path(scheduler.__file__).parent, root / 'appbuilder', ignore=shutil.ignore_patterns('__pycache__'))
            (root / 'config.yaml').write_text('build:\n  max_parallel: 10\n')
            env = dict(os.environ, APP_BUILDER_CONFIG=str(root / 'config.yaml'), PYTHONPATH=str(root))
            children = []
            scheduler_pid = None
            log = (root / 'process.log').open('wb')
            def spawn(*args):
                p = subprocess.Popen([sys.executable, '-m', 'appbuilder', *args], cwd=root, env=env,
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=log)
                children.append(p)
                return p
            def heartbeat():
                try:
                    return json.loads((root / '.appbuilder/scheduler.json').read_text())
                except (OSError, ValueError):
                    return {}
            def wait_for(predicate):
                deadline = time.monotonic() + 40
                while time.monotonic() < deadline:
                    if predicate():
                        return
                    time.sleep(.05)
                self.fail((root / 'process.log').read_text())
            def port():
                with socket.socket() as sock:
                    sock.bind(('127.0.0.1', 0))
                    return sock.getsockname()[1]
            try:
                # Stale state must not prevent automatic startup, nor signal its PID.
                (root / '.appbuilder').mkdir()
                (root / '.appbuilder/scheduler.json').write_text(json.dumps({'pid': 99999999, 'time': time.time()}))
                first = spawn('web', '--port', str(port()), '--no-open')
                second = spawn('web', '--port', str(port()), '--no-open')
                wait_for(lambda: heartbeat().get('pid') != 99999999 and heartbeat().get('instance'))
                scheduler_pid = heartbeat()['pid']
                wait_for(lambda: '本地管理页面' in (root / 'process.log').read_text())
                time.sleep(.5)
                self.assertIsNone(first.poll())
                self.assertIsNone(second.poll())
                self.assertEqual(heartbeat()['pid'], scheduler_pid)
                # A manual foreground instance cannot acquire the runtime lock.
                duplicate = spawn('scheduler', 'run')
                duplicate.wait(timeout=5)
                self.assertIn('已经在运行', (root / 'process.log').read_text())
                self.assertEqual((root / '.appbuilder/scheduler.log').read_text().count('[SCHEDULER] 启动'), 1)
                for p in (first, second):
                    p.send_signal(signal.SIGINT)
                    p.wait(timeout=5)
                os.kill(scheduler_pid, 0)
                third = spawn('web', '--port', str(port()), '--no-open')
                wait_for(lambda: (root / 'process.log').read_text().count('本地管理页面') >= 3)   # slow runners
                time.sleep(.3)
                self.assertIsNone(third.poll())
                self.assertEqual(heartbeat()['pid'], scheduler_pid)
                self.assertEqual((root / 'process.log').read_text().count('本地管理页面'), 3)
            finally:
                for p in children:
                    if p.poll() is None:
                        p.terminate()
                        p.wait(timeout=5)
                # Only the scheduler created in this private copied checkout is stopped.
                if scheduler_pid and heartbeat().get('config') == str(root / 'config.yaml'):
                    try:
                        os.kill(scheduler_pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                log.close()
