"""Read-only kernel process identities; signalling always rechecks the instance."""
import ctypes
import os
from pathlib import Path
import signal
import subprocess
import sys


def birth(pid):
    if sys.platform == 'darwin':
        class BSDInfo(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint32) for name in ('flags','status','xstatus','pid','ppid','uid','gid','ruid','rgid','svuid','svgid','rfu')] + [('comm',ctypes.c_char*16),('name',ctypes.c_char*32)] + [(name,ctypes.c_uint32) for name in ('nfiles','pgid','pjobc','tdev','tpgid','nice')] + [('seconds',ctypes.c_uint64),('microseconds',ctypes.c_uint64)]
        library = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
        info = BSDInfo()
        count = library.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
        if count != ctypes.sizeof(info) or info.status == 5: raise ProcessLookupError(pid)
        return (info.pid, info.uid, info.seconds, info.microseconds)
    text = Path(f'/proc/{pid}/stat').read_text()
    fields = text[text.rfind(')')+2:].split()
    if fields[0] == 'Z': raise ProcessLookupError(pid)
    return (pid, Path(f'/proc/{pid}').stat().st_uid, fields[19])


def cwd(pid):
    if sys.platform != 'darwin': return Path(os.readlink(f'/proc/{pid}/cwd')).resolve()
    result = subprocess.run(['lsof','-a','-p',str(pid),'-d','cwd','-Fn'], capture_output=True,text=True)
    values = [line[1:] for line in result.stdout.splitlines() if line.startswith('n')]
    if result.returncode or len(values) != 1: raise RuntimeError(f'无法核实进程 {pid} 工作目录')
    return Path(values[0]).resolve()


def executable(pid):
    if sys.platform != 'darwin': return os.readlink(f'/proc/{pid}/exe')
    library = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
    buffer = ctypes.create_string_buffer(4096)
    count = library.proc_pidpath(pid, buffer, len(buffer))
    if count <= 0: raise ProcessLookupError(pid)
    return os.fsdecode(buffer.value)


def argv(pid):
    if sys.platform != 'darwin':
        return [os.fsdecode(value) for value in Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0') if value]
    library = ctypes.CDLL(None, use_errno=True)
    mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
    size = ctypes.c_size_t(262144)
    buffer = ctypes.create_string_buffer(size.value)
    if library.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) != 0:
        raise ProcessLookupError(pid)
    raw = buffer.raw[:size.value]
    count = ctypes.c_int.from_buffer_copy(raw[:4]).value
    index = raw.index(b'\0', 4) + 1
    while index < len(raw) and raw[index] == 0: index += 1
    return [os.fsdecode(value) for value in raw[index:].split(b'\0')[:count]]


def arguments(record):
    import shlex
    if 'argv' in record:
        if record['argv'] is None:
            if '-m appbuilder' in record['command']:
                raise RuntimeError('无法核实后台 appbuilder 参数，停止受阻')
            return []
        return record['argv']
    return shlex.split(record['command'])


def snapshot():
    result = subprocess.run(['ps','-axo','pid=,ppid=,command='],capture_output=True,text=True)
    if result.returncode: raise RuntimeError('无法核实后台进程')
    records = {}
    for line in result.stdout.splitlines():
        fields = line.strip().split(None,2)
        if len(fields) != 3: continue
        pid,parent = map(int,fields[:2])
        try:
            token = birth(pid)
            try: exe = executable(pid)
            except (PermissionError, ProcessLookupError): exe = None
            try: args = argv(pid)
            except (PermissionError, ProcessLookupError): args = None
        except (FileNotFoundError, ProcessLookupError): continue
        records[pid] = {'pid':pid,'parent':parent,'command':fields[2],'birth':token,'exe':exe,'argv':args}
    return records


def owned(root):
    import shlex
    records = snapshot()
    roots = set()
    for pid, record in records.items():
        if pid == os.getpid() or record['birth'][1] != os.getuid(): continue
        try: args = arguments(record)
        except ValueError: continue
        if len(args) < 4 or args[1:3] != ['-m','appbuilder']: continue
        if args[3] not in ('worker','web','scheduler','run','resume','diagnose-jenkins','login'): continue
        try: directory = cwd(pid)
        except (FileNotFoundError,ProcessLookupError): continue
        if directory == root.resolve(): roots.add(pid)
    for pid in list(roots):
        parent = records.get(records[pid]['parent'])
        if parent:
            try: args = arguments(parent)
            except ValueError: args = []
            if args and Path(args[0]).name == 'caffeinate' and '-m' in args and 'appbuilder' in args:
                try:
                    if cwd(parent['pid']) == root.resolve() and parent['birth'][1] == os.getuid(): roots.add(parent['pid'])
                except (FileNotFoundError,ProcessLookupError): pass
    selected = set(roots)
    blocked = set()
    automation = set()
    while True:
        added = set()
        for pid, record in records.items():
            if pid in selected or pid in blocked or record['parent'] not in selected: continue
            if record['birth'][1] != os.getuid(): continue
            command = record['command']
            try: args = arguments(record)
            except ValueError:
                blocked.add(pid); continue
            if len(args) >= 4 and args[1:3] == ['-m','appbuilder']:
                try:
                    if cwd(pid) != root.resolve():
                        blocked.add(pid); continue
                except (FileNotFoundError,ProcessLookupError): continue
            browser = any(name in (record.get('exe') or command).lower() for name in ('chrome','chromium','firefox','webkit','safari'))
            dedicated = ('--remote-debugging-pipe' in args and any(arg.startswith('--user-data-dir=') and 'playwright' in arg for arg in args))
            if browser and not dedicated and record['parent'] not in automation:
                blocked.add(pid); continue
            if dedicated or record['parent'] in automation: automation.add(pid)
            added.add(pid)
        if not added: break
        selected.update(added)
    # A fresh instance check also protects enumeration against PID recycling.
    return [records[pid] for pid in selected if same(records[pid])]


def same(record):
    try: return birth(record['pid']) == tuple(record['birth'])
    except (FileNotFoundError, ProcessLookupError): return False


def terminate(record, force=False):
    if record['pid'] == os.getpid(): raise RuntimeError('拒绝停止当前控制进程')
    if not same(record): return False
    try: os.kill(record['pid'], signal.SIGKILL if force else signal.SIGTERM)
    except ProcessLookupError: return False
    return True
