import json
import os
from pathlib import Path
import tempfile
import unittest

from appbuilder.edits import update_mobile_ci, update_lock_from_hashes
from appbuilder.gitops import git, publish_gmx
from appbuilder.versions import ARTIFACTS


class GmxPublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.remote, self.repo = root / 'remote.git', root / 'work'
        self.env = dict(os.environ, GIT_AUTHOR_NAME='Fixture', GIT_AUTHOR_EMAIL='fixture@example.invalid',
                        GIT_COMMITTER_NAME='Fixture', GIT_COMMITTER_EMAIL='fixture@example.invalid')
        # commit_and_push intentionally inherits process identity, as production does.
        from unittest.mock import patch
        self.ident = patch.dict(os.environ, self.env)
        self.ident.start()
        self.addCleanup(self.ident.stop)
        git(['init', '--bare', str(self.remote)])
        git(['init', str(self.repo)])
        self.version = '1.3.1'
        self.hashes = {name: str(i + 1) * 64 for i, name in enumerate(ARTIFACTS)}
        body = json.dumps([dict(group='paybybank-ios-plugin', library=name,
                               version=self.version, sha256=self.hashes[name]) for name in ARTIFACTS]) + '\n'
        for name in ('mobile-ci.json', 'mobile-ci.lock'):
            (self.repo / name).write_text(body)
        (self.repo / 'code.txt').write_text('source\n')
        git(['add', '.'], cwd=self.repo)
        git(['commit', '-m', 'source'], cwd=self.repo)
        self.base = git(['rev-parse', 'HEAD'], cwd=self.repo)
        self.target = 'upload/AUTO-test'
        git(['checkout', '-b', self.target], cwd=self.repo)
        git(['remote', 'add', 'origin', str(self.remote)], cwd=self.repo)

    def publish(self):
        return publish_gmx(self.repo, self.target, self.base, self.version, self.hashes, self.env)

    def test_zero_diff_publishes_source_and_resumes_without_commit(self):
        self.assertEqual(self.publish(), self.base)
        self.assertEqual(self.publish(), self.base)
        self.assertEqual(git(['rev-list', '--count', 'HEAD'], cwd=self.repo), '1')

    def test_lock_only_commit_recovers(self):
        self.hashes = {name: 'a' * 64 for name in ARTIFACTS}
        update_lock_from_hashes(self.repo / 'mobile-ci.lock', self.version, self.hashes)
        sha = self.publish()
        self.assertNotEqual(sha, self.base)
        self.assertEqual(self.publish(), sha)

    def test_json_only_commit_recovers(self):
        # Seed source JSON with older values while lock already matches.
        update_mobile_ci(self.repo / 'mobile-ci.json', 'old')
        git(['add', '.'], cwd=self.repo)
        git(['commit', '-m', 'older json'], cwd=self.repo)
        self.base = git(['rev-parse', 'HEAD'], cwd=self.repo)
        update_mobile_ci(self.repo / 'mobile-ci.json', self.version)
        sha = self.publish()
        self.assertEqual(self.publish(), sha)

    def test_both_files_commit_recovers(self):
        self.version = '1.3.2'
        update_mobile_ci(self.repo / 'mobile-ci.json', self.version)
        update_lock_from_hashes(self.repo / 'mobile-ci.lock', self.version, self.hashes)
        sha = self.publish()
        self.assertEqual(self.publish(), sha)

    def test_unrelated_file_is_rejected(self):
        (self.repo / 'code.txt').write_text('unexpected\n')
        with self.assertRaises(ValueError):
            self.publish()

    def test_lock_formatting_rewrite_is_rejected(self):
        path = self.repo / 'mobile-ci.lock'
        path.write_text(json.dumps(json.loads(path.read_text()), indent=2))
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual(git(['ls-remote', 'origin'], cwd=self.repo), '')

    def test_wrong_hash_on_unchanged_tree_is_rejected(self):
        self.hashes = {name: 'f' * 64 for name in ARTIFACTS}
        with self.assertRaises(ValueError):
            self.publish()

    def test_foreign_target_is_not_overwritten(self):
        git(['push', 'origin', 'HEAD:refs/heads/' + self.target], cwd=self.repo)
        (self.repo / 'code.txt').write_text('foreign\n')
        git(['add', '.'], cwd=self.repo)
        git(['commit', '-m', 'human'], cwd=self.repo)
        foreign = git(['rev-parse', 'HEAD'], cwd=self.repo)
        git(['push', 'origin', 'HEAD:refs/heads/' + self.target], cwd=self.repo)
        git(['reset', '--hard', self.base], cwd=self.repo)
        with self.assertRaises(RuntimeError):
            self.publish()
        self.assertTrue(git(['ls-remote', 'origin'], cwd=self.repo).startswith(foreign))
