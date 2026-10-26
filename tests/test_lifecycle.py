import contextlib
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, Mock
from appbuilder import lifecycle, process_identity
REAL_DISABLE = lifecycle.disable_launchd

class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.data = Path(self.tmp.name)
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(patch.object(lifecycle, 'DATA', self.data))
        self.stack.enter_context(patch.object(lifecycle, 'disable_launchd', return_value=False))
        self.addCleanup(self.tmp.cleanup); self.addCleanup(self.stack.close)

    def test_atomic_gate_waits_only_for_admission_critical_section(self):
        entered=threading.Event(); release=threading.Event()
        def operation():
            with lifecycle.admission():
                with lifecycle.admission(): entered.set(); release.wait(2)
        thread=threading.Thread(target=operation); thread.start(); entered.wait(2)
        with patch.object(process_identity,'owned',return_value=[]):
            stop=threading.Thread(target=lambda:lifecycle.stop(.01)); stop.start()
            time.sleep(.02); self.assertEqual(lifecycle.state()['mode'],'RUNNING')
            release.set(); thread.join(2); stop.join(2)
        with self.assertRaises(RuntimeError):
            with lifecycle.admission(): pass

    def test_stop_does_not_wait_for_worker_success_or_mail_activity(self):
        record={'pid':123,'birth':(123,os.getuid(),1)}
        handle=lifecycle.reserve_activity()
        with patch.object(process_identity,'owned',return_value=[record]), patch.object(process_identity,'terminate') as terminate, patch.object(process_identity,'same',return_value=False), patch.object(lifecycle,'busy_workers',side_effect=AssertionError('must not wait')):
            lifecycle.stop(.01)
            self.assertEqual(terminate.call_args_list[0].args,(record,))
        handle.close(); self.assertTrue(lifecycle.should_exit())

    def test_stop_twice_is_safe_and_keeps_state_files(self):
        sentinel=self.data/'state.json'; sentinel.write_text('unknown receipt')
        with patch.object(process_identity,'owned',return_value=[]):
            lifecycle.stop(.01); lifecycle.stop(.01)
        self.assertEqual(sentinel.read_text(),'unknown receipt')
        lifecycle.resume(); self.assertEqual(lifecycle.state()['mode'],'RUNNING')

    def test_nested_different_project_gate_is_checked(self):
        other=self.data/'other'
        with lifecycle.control(other): lifecycle._save({'mode':'STOPPED'},other)
        with lifecycle.admission():
            with self.assertRaises(RuntimeError):
                with lifecycle.admission(other): pass

    def test_pid_reuse_never_signals(self):
        record={'pid':123,'birth':(123,os.getuid(),1)}
        with patch.object(process_identity,'birth',return_value=(123,os.getuid(),2)),patch.object(process_identity.os,'kill') as kill:
            self.assertFalse(process_identity.terminate(record)); self.assertFalse(process_identity.terminate(record,True)); kill.assert_not_called()

    def test_only_current_project_and_descendants_selected(self):
        uid=os.getuid()
        records={101:{'pid':101,'parent':1,'command':'python -m appbuilder worker task','birth':(101,uid,1)},102:{'pid':102,'parent':101,'command':'chrome --remote-debugging-pipe --user-data-dir=/tmp/playwright_chromiumdev_profile-example','birth':(102,uid,1)},103:{'pid':103,'parent':101,'command':'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome --profile-directory=Default','birth':(103,uid,1)},104:{'pid':104,'parent':1,'command':'python -m appbuilder web','birth':(104,uid,1)}}
        with patch.object(process_identity,'snapshot',return_value=records),patch.object(process_identity,'same',return_value=True),patch.object(process_identity,'cwd',side_effect=lambda pid:self.data if pid==101 else Path('/other')):
            self.assertEqual({r['pid'] for r in process_identity.owned(self.data)},{101,102})

    def test_launchd_prefix_project_is_not_accepted(self):
        import plistlib
        from appbuilder import scheduler
        path=self.data/'agent.plist'
        path.write_bytes(plistlib.dumps({'Label':scheduler.LABEL,'WorkingDirectory':str(lifecycle.ROOT),'ProgramArguments':['python','-m','appbuilder','scheduler','run']}))
        with patch.object(lifecycle.sys,'platform','darwin'),patch.object(scheduler,'plist_path',return_value=path),patch.object(lifecycle,'_launchctl',return_value=Mock(returncode=0,stdout=f'path = {path}\nworking directory = {lifecycle.ROOT}-other')) as call:
            with self.assertRaises(RuntimeError): REAL_DISABLE()
            self.assertEqual(call.call_count,1)

    def test_previously_disabled_launchd_is_not_enabled_by_resume(self):
        from appbuilder import scheduler
        with patch.object(lifecycle,'launchd_disk_identity',return_value={'path':'safe','sha256':'hash','root':'root'}),patch.object(lifecycle,'owned_launchd',return_value='target'),patch.object(lifecycle,'_launchctl',return_value=Mock(returncode=0,stdout=f'"{scheduler.LABEL}" => true')) as call:
            self.assertFalse(REAL_DISABLE()); self.assertEqual(call.call_count,2)
            lifecycle.resume(); self.assertEqual(call.call_count,2)

    def test_resume_revalidates_launchd_ownership(self):
        with lifecycle.control(): lifecycle._save({'mode':'STOPPED','launchd_disabled':True})
        with patch.object(lifecycle,'owned_launchd',side_effect=RuntimeError('foreign')),patch.object(lifecycle,'_launchctl') as call:
            with self.assertRaises(RuntimeError): lifecycle.resume()
            call.assert_not_called()
        self.assertEqual(lifecycle.state()['mode'],'STOPPED')

    def test_launchd_disabled_before_any_signal(self):
        events=[]
        with patch.object(lifecycle,'disable_launchd',side_effect=lambda:events.append('disable') or True),patch.object(process_identity,'owned',side_effect=lambda root:events.append('inventory') or []): lifecycle.stop(.01)
        self.assertEqual(events,['inventory','disable'])

    def test_real_project_process_and_child_stop_without_touching_other_project(self):
        import subprocess
        import sys
        package=self.data/'appbuilder'; package.mkdir()
        (package/'__init__.py').write_text('')
        (package/'__main__.py').write_text("import subprocess,sys,time\np=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\nprint(p.pid,flush=True)\ntime.sleep(60)\n")
        interpreter=self.data/'Python With Spaces'
        interpreter.symlink_to(sys.executable)
        process=subprocess.Popen([str(interpreter),'-m','appbuilder','worker','example'],cwd=self.data,stdout=subprocess.PIPE,text=True)
        other=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])
        child=int(process.stdout.readline())
        try:
            with patch.object(lifecycle,'ROOT',self.data): lifecycle.stop(.2)
            process.wait(timeout=3)
            self.assertIsNone(other.poll())
            with self.assertRaises((ProcessLookupError,FileNotFoundError)): process_identity.birth(child)
        finally:
            process.stdout.close()
            if process.poll() is None: process.kill(); process.wait()
            other.terminate(); other.wait()

    def test_store_create_resume_block_before_mutation(self):
        from appbuilder import store
        with lifecycle.control(): lifecycle._save({'mode':'STOPPED'})
        with patch.object(store,'DATA',self.data),patch.object(store,'connect') as connect:
            with self.assertRaises(RuntimeError): store.create_task({}, {})
            with self.assertRaises(RuntimeError): store.reserve_resume({}, 'task')
            connect.assert_not_called()

    def test_failed_launchd_disable_is_retried_before_process_signals(self):
        with lifecycle.control(): lifecycle._save({'mode':'STOPPING','launchd_disable_pending':True})
        with patch.object(lifecycle,'disable_launchd',side_effect=RuntimeError('denied')) as disable,patch.object(process_identity,'owned') as owned:
            with self.assertRaises(RuntimeError): lifecycle.stop(.01)
            disable.assert_called_once()

    def test_interrupted_launchd_disable_keeps_restore_ownership(self):
        with lifecycle.control(): lifecycle._save({'mode':'STOPPING','launchd_disable_pending':True})
        with patch.object(lifecycle,'disable_launchd',return_value=False),patch.object(process_identity,'owned',return_value=[]): lifecycle.stop(.01)
        self.assertTrue(lifecycle.state()['launchd_disabled'])

    def test_launchd_disable_bootout_then_verified_restore_bootstrap(self):
        identity={'path':'/safe/agent.plist','sha256':'verified','root':str(lifecycle.ROOT)}
        with patch.object(lifecycle,'owned_launchd',return_value='target'),patch.object(lifecycle,'launchd_disk_identity',return_value=identity),patch.object(lifecycle,'_launchctl',return_value=Mock(returncode=0,stdout='')) as call:
            self.assertTrue(REAL_DISABLE())
            self.assertEqual([c.args[0] for c in call.call_args_list],['print-disabled','disable','bootout'])
        with patch.object(lifecycle,'owned_launchd',return_value=False),patch.object(lifecycle,'launchd_disk_identity',return_value=identity),patch.object(lifecycle,'_launchctl',return_value=Mock(returncode=0,stdout='')) as call:
            lifecycle.resume()
            self.assertEqual([c.args[0] for c in call.call_args_list],['enable','bootstrap'])
        self.assertEqual(lifecycle.state()['mode'],'RUNNING')

    def test_restore_rejects_replaced_plist_after_bootout(self):
        with lifecycle.control(): lifecycle._save({'mode':'STOPPED','launchd_disabled':True,'launchd_identity':{'sha256':'original'}})
        with patch.object(lifecycle,'owned_launchd',return_value=False),patch.object(lifecycle,'launchd_disk_identity',return_value={'sha256':'changed'}),patch.object(lifecycle,'_launchctl') as call:
            with self.assertRaises(RuntimeError): lifecycle.resume()
            call.assert_not_called()

    def test_interrupted_restore_retries_bootstrap_with_saved_identity(self):
        identity={'path':'/safe/agent.plist','sha256':'verified','root':str(lifecycle.ROOT)}
        with lifecycle.control(): lifecycle._save({'mode':'STOPPED','launchd_disabled':True,'launchd_identity':identity})
        def interrupted(*args):
            if args[0] == 'bootstrap': raise KeyboardInterrupt()
            return Mock(returncode=0,stdout='')
        with patch.object(lifecycle,'owned_launchd',return_value=False),patch.object(lifecycle,'launchd_disk_identity',return_value=identity),patch.object(lifecycle,'_launchctl',side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt): lifecycle.resume()
        self.assertTrue(lifecycle.state()['launchd_restore_pending'])
        self.assertEqual(lifecycle.state()['launchd_identity'],identity)
        with patch.object(lifecycle,'owned_launchd',return_value=False),patch.object(lifecycle,'launchd_disk_identity',return_value=identity),patch.object(lifecycle,'_launchctl',return_value=Mock(returncode=0)) as call:
            lifecycle.resume()
            self.assertEqual([c.args[0] for c in call.call_args_list],['enable','bootstrap'])
        self.assertEqual(lifecycle.state(),{'mode':'RUNNING'})
