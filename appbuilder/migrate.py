"""Optional safe local upgrade from an older downloaded ZIP directory.

Not part of Jenkins or build workflow. Never deletes the source.
"""
from __future__ import annotations
import argparse
import shutil
import subprocess
from pathlib import Path

from .config import ROOT


def import_state(old_dir: str | Path, new_dir: str | Path = ROOT):
    source = Path(old_dir).expanduser().resolve()
    dest = Path(new_dir).expanduser().resolve()
    if source == dest:
        raise ValueError('请选择旧版本目录，而不是新版本目录')
    data = source / '.appbuilder'
    if not data.is_dir() or data.is_symlink():
        raise ValueError('旧目录中没有有效的 .appbuilder 数据目录。请先确认路径')
    output = dest / '.appbuilder'
    if output.exists() or output.is_symlink():
        raise ValueError('新目录已经有 .appbuilder，为避免覆盖现有任务，已停止导入')
    if source in dest.parents or dest in source.parents:
        raise ValueError('不能从上级/下级的嵌套目录导入状态')
    # Copy, never move. Explicit user request only; login cookies stay local.
    shutil.copytree(data, output, symlinks=True)
    return output


def main(argv=None):
    p=argparse.ArgumentParser(description='从旧 ZIP 解压目录复制任务、日志和 Jenkins SSO 缓存')
    p.add_argument('old_dir',nargs='?',help='旧版本 mac-app-builder 的解压目录')
    a=p.parse_args(argv)
    if a.old_dir:
        source=a.old_dir
    else:
        try:
            r=subprocess.run(['osascript','-e',
                'POSIX path of (choose folder with prompt "选择旧版 APP Builder 所在的文件夹")'],
                capture_output=True,text=True,check=True,timeout=90)
            source=r.stdout.strip()
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            p.error('无法打开 Finder 文件夹选择器；请在命令行直接传入旧版本目录')
    try:
        new=import_state(source)
    except ValueError as e:
        p.error(str(e))
    print(f'[MIGRATE] 已安全复制旧任务数据到 {new}')
    print('[MIGRATE] 不会删除旧目录。请手动比对旧版 config.yaml 的自定义设置。')
    print('[MIGRATE] 确保旧版 Worker 已停止，才能在新版本中恢复任务。')

if __name__=='__main__':main()
