"""Real temporary repositories; HTTPS URL rewritten only by test Git config."""
import json
import os
from pathlib import Path
import tempfile
import unittest

from appbuilder.gitops import git
from appbuilder.edits import XCCONFIGS
from appbuilder.plugin_git import prepare, remote_tip, PluginPreparationError

URL = 'https://plugin.fixture.invalid/repo.git'
TARGET = 'release/v1.3.1'


class PluginGitPrepareTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name);self.bare=self.home/'origin.git';self.source=self.home/'source'
        settings=[('url.'+str(self.bare)+'.insteadOf',URL),('protocol.file.allow','always'),
                  ('user.name','Fixture'),('user.email','fixture@example.invalid')]
        self.env=dict(os.environ,GIT_CONFIG_COUNT=str(len(settings)))
        for i,(key,value) in enumerate(settings):
            self.env['GIT_CONFIG_KEY_'+str(i)]=key;self.env['GIT_CONFIG_VALUE_'+str(i)]=value
        git(['init','--bare',str(self.bare)],env=self.env)
        git(['init','-b','develop',str(self.source)],env=self.env)
        git(['remote','add','origin',URL],cwd=self.source,env=self.env)
        for rel in XCCONFIGS:
            p=self.source/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('BUNDLE_VERSION_SHORT = 1.3.0\n')
        (self.source/'code.txt').write_text('original\n')
        self.commit_source('initial')

    def commit_source(self,message):
        git(['add','.'],cwd=self.source,env=self.env)
        git(['commit','-m',message],cwd=self.source,env=self.env)
        git(['push','origin','HEAD:refs/heads/develop'],cwd=self.source,env=self.env)
        return git(['rev-parse','HEAD'],cwd=self.source,env=self.env)

    def run_prepare(self,name,progress=None,checkpoint=None):
        states=[]
        def save(s):
            states.append(json.loads(json.dumps(s)))
            if checkpoint:checkpoint(s)
        result=prepare(self.home/name,URL,'develop',TARGET,'1.3.1',self.env,progress,save)
        return result,states

    def human(self,filename,content):
        root=self.home/'human'
        if not root.exists():git(['clone','--branch',TARGET,URL,str(root)],env=self.env)
        (root/filename).write_text(content)
        git(['add','.'],cwd=root,env=self.env);git(['commit','-m','human'],cwd=root,env=self.env)
        git(['push','origin','HEAD:refs/heads/'+TARGET],cwd=root,env=self.env)
        return git(['rev-parse','HEAD'],cwd=root,env=self.env)

    def test_source_equal_target_stops_before_changing_remote(self):
        initial, _ = self.run_prepare('seed')
        with self.assertRaises(PluginPreparationError):
            prepare(self.home/'blocked', URL, TARGET, TARGET, '1.3.1', self.env, None, lambda _: None)
        self.assertEqual(remote_tip(URL, TARGET, self.env), initial['build_sha'])
        self.assertFalse((self.home/'blocked').exists())

    def test_two_runs_merge_updated_develop_and_keep_fixed_version(self):
        first,_=self.run_prepare('one')
        (self.source/'code.txt').write_text('updated\n');source=self.commit_source('new code')
        second,_=self.run_prepare('two')
        self.assertEqual(second['source_sha'],source)
        self.assertNotEqual(second['source_sha'],second['build_sha'])
        self.assertEqual(second['target_before'],first['build_sha'])
        self.assertEqual((self.home/'two'/'code.txt').read_text(),'updated\n')
        for p in XCCONFIGS:self.assertEqual((self.home/'two'/p).read_text(),'BUNDLE_VERSION_SHORT = 1.3.1\n')
        self.assertEqual(remote_tip(URL,TARGET,self.env),second['build_sha'])

    def test_source_code_with_trailing_whitespace_does_not_stop_the_merge(self):
        # Real case: develop gained Swift files with trailing spaces / blank line at EOF;
        # `git diff --cached --check` on the whole merge failed with exit 2.
        first,_=self.run_prepare('one')
        (self.source/'Code.swift').write_text('let a = 1   \n//  \n\n');source=self.commit_source('teammate code')
        second,_=self.run_prepare('two')
        self.assertEqual(second['source_sha'],source)
        self.assertEqual(second['target_before'],first['build_sha'])
        self.assertEqual((self.home/'two'/'Code.swift').read_text(),'let a = 1   \n//  \n\n')   # their code untouched
        self.assertEqual(remote_tip(URL,TARGET,self.env),second['build_sha'])

    def test_unchanged_second_run_does_not_create_empty_commit(self):
        first,_=self.run_prepare('one');second,_=self.run_prepare('two')
        self.assertEqual(first['build_sha'],second['build_sha'])
        self.assertEqual(first['source_sha'],second['source_sha'])

    def test_real_target_human_commit_is_preserved_on_source_update(self):
        self.run_prepare('one');human=self.human('human.txt','keep\n')
        (self.source/'code.txt').write_text('updated\n');self.commit_source('new code')
        second,_=self.run_prepare('two')
        self.assertEqual(second['target_before'],human)
        self.assertEqual((self.home/'two'/'human.txt').read_text(),'keep\n')
        self.assertIn(human,git(['rev-list',second['build_sha']],cwd=self.home/'two',env=self.env).splitlines())

    def test_conflict_stops_without_changing_remote_or_discarding_work(self):
        self.run_prepare('one');human=self.human('code.txt','human\n')
        (self.source/'code.txt').write_text('source\n');self.commit_source('conflict')
        with self.assertRaises(RuntimeError):self.run_prepare('two')
        self.assertEqual(remote_tip(URL,TARGET,self.env),human)
        self.assertTrue((self.home/'two'/'.git'/'MERGE_HEAD').exists())
        self.assertIn('<<<<<<<',(self.home/'two'/'code.txt').read_text())

    def test_crash_after_push_resumes_without_new_commit(self):
        first,states=self.run_prepare('one')
        resumed,_=self.run_prepare('one',states[-2])
        self.assertEqual(first['build_sha'],resumed['build_sha'])

    def test_resume_keeps_frozen_source_when_develop_advances(self):
        saved=[]
        def stop(s):
            saved.append(s)
            if s['phase']=='PLANNED':raise RuntimeError('fixture stop')
        with self.assertRaises(RuntimeError):self.run_prepare('one',checkpoint=stop)
        (self.source/'code.txt').write_text('later\n');self.commit_source('later source')
        result,_=self.run_prepare('one',saved[-1])
        self.assertEqual(result['source_sha'],saved[-1]['source_sha'])
        self.assertEqual((self.home/'one'/'code.txt').read_text(),'original\n')

    def test_external_target_move_after_prepare_blocks_push(self):
        saved=[]
        def stop(s):
            saved.append(s)
            if s['phase']=='PUSH_READY':raise RuntimeError('fixture stop')
        first,_=self.run_prepare('first')
        (self.source/'code.txt').write_text('later\n');self.commit_source('later source')
        with self.assertRaises(RuntimeError):self.run_prepare('second',checkpoint=stop)
        human=self.human('human.txt','external\n')
        with self.assertRaisesRegex(PluginPreparationError,'外部提交'):self.run_prepare('second',saved[-1])
        self.assertEqual(remote_tip(URL,TARGET,self.env),human)
