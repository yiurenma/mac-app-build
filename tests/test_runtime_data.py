import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from appbuilder import runtime_data as rd

TASK='202610141200-abcdef'
class RuntimeDataTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.folder=self.root/'runs'/TASK;self.folder.mkdir(parents=True)
        self.path=self.folder/'state.json'
        self.state={'completed':['PLUGIN_PREPARE'],'runtime':{'plugin_formal_number':102,'plugin_callback_number':103,'plugin_formal_result':'SUCCESS','step_errors':{'PLUGIN_JENKINS':'Callback failure'},'password':'supersecret','mail_preview':{'body':'private body'},'url':'https://user:pw@example.invalid/a?signature=secret#private','contact':'person@example.com','message':'Authorization: Bearer abcdef'}}
        self.path.write_text(json.dumps(self.state));self.lookup=lambda key:{'id':key,'status':'FAILED','stage':'PLUGIN_JENKINS','created':'2026-10-14'}
    def snap(self):return rd.snapshot(TASK,self.root,self.lookup)
    def test_snapshot_redacts_and_preserves_failure(self):
        result=self.snap();text=json.dumps(result)
        for secret in ['supersecret','private body','user:pw','signature=secret','person@example.com','Bearer abcdef']:self.assertNotIn(secret,text)
        self.assertEqual(result['task']['status'],'FAILED');self.assertEqual(result['state']['runtime']['plugin_callback_number'],103)
        self.assertEqual(json.loads(self.path.read_text()),self.state)
    def test_invalid_and_unknown_tasks(self):
        for key in ['../abc','%2e%2e','202610141200-abcdef/..','202610141200-abcdef\n']:
            with self.assertRaises(ValueError):rd.snapshot(key,self.root,self.lookup)
        with self.assertRaises(ValueError):rd.snapshot(TASK,self.root,lambda _:None)
    def test_file_and_directory_symlinks_rejected(self):
        other=self.root/'private';other.write_text(json.dumps(self.state));self.path.unlink();self.path.symlink_to(other)
        with self.assertRaises(ValueError):self.snap()
        self.path.unlink();self.folder.rmdir();self.folder.symlink_to(self.root,target_is_directory=True)
        with self.assertRaises(ValueError):self.snap()
    def test_missing_corrupt_oversize_nonregular(self):
        for data in ['broken','[]','{"runtime": [], "completed": []}','x'*(rd.MAX_BYTES+1),'{"completed":[],"runtime":{"x":NaN}}']:
            self.path.write_text(data)
            with self.assertRaises(ValueError):self.snap()
        self.path.unlink();os.mkfifo(self.path)
        with self.assertRaises(ValueError):self.snap()
    def test_limits_and_unknown_nested_secrets(self):
        with self.assertRaises(ValueError):rd.sanitize([0]*10001)
        deep={}
        for _ in range(25):deep={'x':deep}
        with self.assertRaises(ValueError):rd.sanitize(deep)
        self.assertEqual(rd.sanitize({'nested':[{'access_token':'raw','share_url':'https://t.testfairy.com/join/private'}]})['nested'][0],{'access_token':'[已隐藏]','share_url':'https://t.testfairy.com/join/[已隐藏]'})

    def test_runs_symlink_and_secret_subtree(self):
        import shutil
        shutil.rmtree(self.root/'runs')
        elsewhere=self.root/'elsewhere';elsewhere.mkdir()
        (self.root/'runs').symlink_to(elsewhere,target_is_directory=True)
        with self.assertRaises(ValueError):self.snap()
        self.assertEqual(rd.sanitize({'auth':{'unusual':'secret'},'session':'opaque'}),{'auth':'[已隐藏]','session':'[已隐藏]'})
