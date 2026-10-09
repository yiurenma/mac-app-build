import subprocess
import tempfile
import unittest
from pathlib import Path
from appbuilder.gitops import commit_and_push, git


def gitraw(*args, cwd=None):
    subprocess.run(['git', *args], cwd=cwd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class TestSafeCommit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bare = self.root / 'remote.git'
        self.working = self.root / 'working'
        gitraw('init', '--bare', str(self.bare))
        gitraw('init', str(self.working))
        gitraw('config', 'user.email', 'test@example.com', cwd=self.working)
        gitraw('config', 'user.name', 'App Test', cwd=self.working)
        (self.working / 'allowed.txt').write_text('old\n')
        (self.working / 'other.txt').write_text('keep\n')
        gitraw('add', '.', cwd=self.working)
        gitraw('commit', '-m', 'initial', cwd=self.working)
        gitraw('checkout', '-b', 'release/1.3.test', cwd=self.working)
        gitraw('remote', 'add', 'origin', str(self.bare), cwd=self.working)

    def test_blocks_unexpected_changes(self):
        (self.working / 'allowed.txt').write_text('new\n')
        (self.working / 'other.txt').write_text('accidental\n')
        with self.assertRaises(ValueError):
            commit_and_push(self.working, {'allowed.txt'}, 'version update', 'release/1.3.test', env=None)

    def test_safe_commit_and_remote_verify(self):
        (self.working / 'allowed.txt').write_text('new\n')
        sha = commit_and_push(self.working, {'allowed.txt'}, 'version update', 'release/1.3.test', env=None)
        self.assertEqual(sha, git(['rev-parse', 'HEAD'], cwd=self.working))
        remote = git(['ls-remote', 'origin', 'refs/heads/release/1.3.test'], cwd=self.working)
        self.assertTrue(remote.startswith(sha))

    def test_blocks_hook_modifications(self):
        (self.working / 'allowed.txt').write_text('new\n')
        hook = self.working / '.git' / 'hooks' / 'post-commit'
        hook.write_text('#!/bin/sh\necho modified > other.txt\n')
        hook.chmod(0o755)
        with self.assertRaises(ValueError):
            commit_and_push(self.working, {'allowed.txt'}, 'version update', 'release/1.3.test', env=None)
        self.assertFalse(git(['ls-remote', 'origin', 'refs/heads/release/1.3.test'], cwd=self.working))


if __name__ == '__main__':
    unittest.main()
