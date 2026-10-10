"""Validate exact committed blobs to catch Git hooks rewriting *allowed* files."""
from pathlib import Path
from tempfile import TemporaryDirectory
from .gitops import git_blob
from .edits import XCCONFIGS, edit_plugin_versions, update_mobile_ci, validate_lock, ensure_lock_other_entries_unchanged


def plugin_commit(root: Path, base_sha: str, version: str):
    with TemporaryDirectory() as td:
        checkroot = Path(td)
        for rel in XCCONFIGS:
            blob = git_blob(root, base_sha, rel)
            p = checkroot / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(blob)
        edit_plugin_versions(checkroot, version)
        for rel in XCCONFIGS:
            expected = (checkroot / rel).read_bytes()
            actual = git_blob(root, 'HEAD', rel)
            if actual != expected:
                raise ValueError(f'{rel} 最终 Commit 含有不符合要求的修改（可能来自 Git Hook），禁止 Push')


def gmx_commit(root: Path, base_sha: str, artifact_version: str, expected_hashes=None):
    with TemporaryDirectory() as td:
        checkroot = Path(td)
        input_json = git_blob(root, base_sha, 'mobile-ci.json')
        f = checkroot / 'mobile-ci.json'
        f.write_bytes(input_json)
        update_mobile_ci(f, artifact_version)
        if git_blob(root, 'HEAD', 'mobile-ci.json') != f.read_bytes():
            raise ValueError('mobile-ci.json Commit 含有版本字段之外的变化，禁止 Push')
        before_lock = git_blob(root, base_sha, 'mobile-ci.lock')
        after_lock = git_blob(root, 'HEAD', 'mobile-ci.lock')
        ensure_lock_other_entries_unchanged(before_lock.decode('utf-8'), after_lock.decode('utf-8'))
        checklock = checkroot / 'mobile-ci.lock'
        checklock.write_bytes(after_lock)
        actual_hashes = validate_lock(checklock, artifact_version)
        if expected_hashes is not None and actual_hashes != expected_hashes:
            raise ValueError('mobile-ci.lock Commit SHA256 与当前 Jenkins Nexus 输出不一致，禁止 Push')
