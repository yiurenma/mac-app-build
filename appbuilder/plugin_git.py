"""Prepare a Plugin target without rewriting remote history.

The caller persists each checkpoint in the task state before proceeding. Resume
uses that frozen source commit; a later source update belongs to the next task.
"""
from pathlib import Path
from urllib.parse import urlsplit

from .gitops import git, status, verify_pushed
from .edits import edit_plugin_versions, XCCONFIGS


class PluginPreparationError(ValueError):
    pass


def remote_tip(url, branch, env):
    rows = git(['ls-remote', '--heads', url, 'refs/heads/' + branch], env=env).splitlines()
    if len(rows) > 1:
        raise PluginPreparationError('远端分支引用不唯一')
    return rows[0].split()[0] if rows else None


def prepare(root: Path, url, source, target, version, env, progress, checkpoint):
    """Merge real target + frozen source, set version, then ordinary push.

    progress belongs to this task only; checkpoint must durably replace it.
    Never infer ownership from a commit message alone.
    """
    if source == target:
        raise PluginPreparationError('Plugin 来源分支不能与构建目标相同，禁止修改来源分支')
    u = urlsplit(url)
    if u.scheme != 'https' or not u.hostname or u.username or u.password:
        raise PluginPreparationError('Plugin 仓库必须为不含凭据的 HTTPS URL')
    state = dict(progress or {})
    identity = {'url': url, 'source': source, 'target': target, 'version': version}
    if state and any(state.get(k) != v for k, v in identity.items()):
        raise PluginPreparationError('恢复参数与已保存的 Plugin 准备记录不一致')
    def save(**changes):
        state.update(changes)
        checkpoint(dict(state))
    if not state:
        source_sha = remote_tip(url, source, env)
        if not source_sha:
            raise PluginPreparationError('Plugin 来源分支不存在')
        save(**identity, source_sha=source_sha, target_before=remote_tip(url, target, env), phase='PLANNED')
    if not root.exists():
        root.parent.mkdir(parents=True, exist_ok=True)
        git(['clone', '--no-checkout', url, str(root)], env=env)
    if not (root / '.git').is_dir() or git(['config', '--get', 'remote.origin.url'], cwd=root) != url:
        raise PluginPreparationError('Plugin 工作目录或 origin 不符合准备记录')
    if state.get('build_sha'):
        if git(['rev-parse', 'HEAD'], cwd=root) != state['build_sha'] or status(root):
            raise PluginPreparationError('恢复工作目录已被修改，禁止继续推送')
    else:
        # Fetch named refs, then verify the frozen SHAs. A moving ref never
        # silently changes the inputs of an interrupted task.
        git(['fetch', '--no-tags', 'origin', '+refs/heads/' + source + ':refs/remotes/origin/' + source], cwd=root, env=env)
        git(['cat-file', '-e', state['source_sha'] + '^{commit}'], cwd=root)
        if state['target_before']:
            git(['fetch', '--no-tags', 'origin', '+refs/heads/' + target + ':refs/remotes/origin/' + target], cwd=root, env=env)
            git(['cat-file', '-e', state['target_before'] + '^{commit}'], cwd=root)
        base = state['target_before'] or state['source_sha']
        if state['phase'] == 'PLANNED':
            if remote_tip(url, target, env) != state['target_before']:
                raise PluginPreparationError('Plugin 目标被外部修改，请重新核对后创建任务')
            if status(root):
                # A fresh --no-checkout clone has index deletions, but no files.
                if any(p.name != '.git' for p in root.iterdir()):
                    raise PluginPreparationError('Plugin 工作目录存在未记录修改')
            git(['checkout', '-B', target, base], cwd=root)
            save(phase='CHECKED_OUT', base_sha=base)
        if state['phase'] == 'CHECKED_OUT':
            if status(root) or (root / '.git' / 'MERGE_HEAD').exists():
                raise PluginPreparationError('存在未完成合并或修改，请人工核对；不自动覆盖')
            if git(['rev-parse', 'HEAD'], cwd=root) != base:
                raise PluginPreparationError('Plugin 本地基线已改变')
            save(phase='MERGING')
        if state['phase'] == 'MERGING':
            if status(root) or (root / '.git' / 'MERGE_HEAD').exists():
                raise PluginPreparationError('合并中断或冲突，保留现场；不自动提交人工修改')
            if state['target_before']:
                # No strategy overrides: real content conflicts stop here.
                git(['merge', '--no-ff', '--no-commit', state['source_sha']], cwd=root, env=env)
            save(phase='MERGED', merge_tree=git(['write-tree'], cwd=root))
        if state['phase'] == 'MERGED':
            if git(['write-tree'], cwd=root) != state['merge_tree'] or git(['diff', '--name-only'], cwd=root):
                raise PluginPreparationError('合并后的工作区已改变，禁止自动提交')
            # Checkpoint the expected version edit before touching the real tree.
            # A crash during the two-file edit is not silently adopted on Resume.
            save(phase='EDITING')
            edit_plugin_versions(root, version)
            git(['add', '--', *XCCONFIGS], cwd=root)
            git(['diff', '--cached', '--check'], cwd=root)
            if git(['diff', '--name-only'], cwd=root):
                raise PluginPreparationError('版本修改后仍有未暂存变更')
            parents = [base]
            merge_head = root / '.git' / 'MERGE_HEAD'
            if merge_head.exists():
                parents.append(merge_head.read_text().strip())
            save(phase='COMMIT_READY', expected_tree=git(['write-tree'], cwd=root), parents=parents,
                 message='build: prepare Plugin ' + version)
        if state['phase'] == 'EDITING':
            raise PluginPreparationError('版本编辑中断，保留现场等待核对；不自动接受部分修改')
        if state['phase'] == 'COMMIT_READY':
            head = git(['rev-parse', 'HEAD'], cwd=root)
            if head == base:
                if git(['write-tree'], cwd=root) != state['expected_tree'] or git(['diff', '--name-only'], cwd=root):
                    raise PluginPreparationError('待提交内容已改变')
                if state['expected_tree'] != git(['rev-parse', 'HEAD^{tree}'], cwd=root) or len(state['parents']) > 1:
                    git(['commit', '-m', state['message']], cwd=root, env=env)
                    head = git(['rev-parse', 'HEAD'], cwd=root)
            if git(['rev-parse', 'HEAD^{tree}'], cwd=root) != state['expected_tree'] or status(root):
                raise PluginPreparationError('提交内容或工作区与已核验文件树不符')
            if head != base:
                actual = git(['rev-list', '--parents', '-n', '1', 'HEAD'], cwd=root).split()[1:]
                if actual != state['parents'] or git(['log', '-1', '--format=%s'], cwd=root) != state['message']:
                    raise PluginPreparationError('恢复提交的父节点或内容不匹配')
            save(build_sha=head, tree=state['expected_tree'], phase='PUSH_READY')
    current = remote_tip(url, target, env)
    if current not in (state['target_before'], state['build_sha']):
        raise PluginPreparationError('Plugin 远端目标出现外部提交，禁止覆盖')
    if current != state['build_sha']:
        git(['push', '-u', 'origin', 'HEAD:refs/heads/' + target], cwd=root, env=env)
    verify_pushed(root, target, state['build_sha'], env)
    save(phase='PUSHED')
    return state
