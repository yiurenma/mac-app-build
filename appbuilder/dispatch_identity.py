"""Receipt-only ownership for new tasks; never infer ownership from Build history/SHA."""
import re
import uuid
from urllib.parse import unquote, urlsplit


def _plain_path(path):
    """Branch jobs can be written job/a%2Fb/ or job/a%252Fb/; compare the decoded path."""
    for _ in range(3):
        decoded = unquote(path)
        if decoded == path:
            break
        path = decoded
    return path


def same_job(first, second):
    """Same Jenkins job, tolerating the two percent-encodings Jenkins uses for branch names."""
    if not isinstance(first, str) or not isinstance(second, str):
        return False
    a, b = urlsplit(first), urlsplit(second)
    return (a.scheme == b.scheme == 'https' and a.netloc == b.netloc and not a.query and not b.query
            and _plain_path(a.path).rstrip('/') == _plain_path(b.path).rstrip('/'))


def build_number(job, url):
    if not isinstance(url,str):return None
    j,u=urlsplit(job),urlsplit(url)
    if (j.scheme!='https' or u.scheme!=j.scheme or u.netloc!=j.netloc or u.username or u.password
            or u.query or u.fragment):return None
    match=re.fullmatch(re.escape(_plain_path(j.path).rstrip('/')+'/')+r'([1-9][0-9]*)/?',_plain_path(u.path))
    return int(match[1]) if match else None


def queue_url(job, queue_id):
    root = urlsplit(job)
    if (root.scheme != 'https' or not root.hostname or root.username or root.password
            or root.query or root.fragment or '/job/' not in root.path
            or type(queue_id) is not int or queue_id < 1):
        return None
    prefix = root.path.split('/job/', 1)[0]
    return f'{root.scheme}://{root.netloc}{prefix}/queue/item/{queue_id}/'


def reconcile(browser, job, task_id, key, read_state, patch_runtime, **parameters):
    from .jenkins import NeedsAttention
    rt=read_state(task_id)['runtime'];receipt=rt.get(key+'_dispatch')
    saved_number=rt.get(key+'_number')
    if receipt:
        if receipt.get('job_url')!=job or not receipt.get('attempt_id'):
            raise NeedsAttention('新流程缺少本次提交回执身份；禁止按编号差集恢复或重发')
        if receipt.get('state')=='PREPARING':
            if saved_number:
                raise NeedsAttention('提交前状态却存在 Build 编号，禁止认领')
            # A durable DISPATCHING checkpoint always precedes the click. Only
            # PREPARING is positive evidence that this attempt did not submit.
        elif receipt.get('state') not in ('ACCEPTED','ATTRIBUTED'):
            raise NeedsAttention('本次提交结果未知或冲突；保留回执，不自动重发')
    elif saved_number:
        raise NeedsAttention('新流程已存编号但没有本次回执；禁止猜测归属或重发')
    if not receipt or receipt['state']=='PREPARING':
        attempt=receipt['attempt_id'] if receipt else uuid.uuid4().hex
        receipt={'job_url':job,'attempt_id':attempt,'state':'PREPARING','policy':'receipt-v1'}
        patch_runtime(task_id,**{key+'_dispatch':receipt})
        def checkpoint(**fields):
            current=read_state(task_id)['runtime'][key+'_dispatch']
            patch_runtime(task_id,**{key+'_dispatch':{**current,**fields}})
        browser.build_dispatch_checkpoint=checkpoint
        browser.build_expected_baseline=None
        browser.build_strict_dispatch=True
        try:
            browser.request_build(job,**parameters)
        finally:
            browser.build_dispatch_checkpoint=None
            browser.build_strict_dispatch=False
        receipt=read_state(task_id)['runtime'][key+'_dispatch']
    if receipt.get('state') not in ('ACCEPTED','ATTRIBUTED') or receipt.get('http_status') not in (201,302,303):
        raise NeedsAttention('没有可归属的成功提交回执；结果未知，不自动重发')
    direct=build_number(job,receipt.get('build_url'))
    identity=read_state(task_id)['runtime'].get(key+'_identity')
    if identity:
        number=build_number(job,identity.get('build_url'))
        if (not number or identity.get('attempt_id')!=receipt['attempt_id']
                or identity.get('job_url')!=job or (saved_number and number!=saved_number)
                or (identity.get('kind')=='DIRECT_BUILD' and direct!=number)
                or (identity.get('kind')=='QUEUE_EXECUTABLE' and
                    (identity.get('queue_id')!=receipt.get('queue_id') or direct
                     or receipt.get('queue_url') != queue_url(job, receipt.get('queue_id'))))
                or identity.get('kind') not in ('DIRECT_BUILD','QUEUE_EXECUTABLE')):
            raise NeedsAttention('持久化构建归属与回执不一致；禁止继续')
        return number
    if direct:
        proof={'kind':'DIRECT_BUILD','build_url':job.rstrip('/')+'/'+str(direct)+'/'}
    else:
        queue=receipt.get('queue_id')
        if not isinstance(queue,int) or isinstance(queue,bool) or queue<1:
            raise NeedsAttention('回执没有直接 Build URL 或有效 queue_id；不猜测编号')
        if receipt.get('queue_url') != queue_url(job, queue):
            raise NeedsAttention('队列回执路径缺失或与本次 Job 的 Jenkins context 不一致；不猜测查询地址')
        resolver=getattr(browser,'resolve_queue_build',None)
        if not callable(resolver):
            raise NeedsAttention('已有 queue_id，但缺少经核实的队列到 Build 提取器；Resume 不会重新提交')
        try:
            observed=resolver(job,queue)
        except NeedsAttention as exc:
            # The queue item is gone (Jenkins drops it ~5 min after it starts) or did not
            # match. Every build records the queue id it came from; that exact link is proof.
            finder=getattr(browser,'find_build_by_queue_id',None)
            observed=finder(job,queue) if callable(finder) else None
            if not observed:
                raise exc
        if (not isinstance(observed,dict) or observed.get('queue_id')!=queue
                or observed.get('job_url')!=job or observed.get('relation')!='executable'
                or not build_number(job,observed.get('build_url'))):
            raise NeedsAttention('未得到该 queue_id 的明确 executable Build 映射；不采用新增编号或相同 SHA')
        proof={'kind':'QUEUE_EXECUTABLE','queue_id':queue,'build_url':observed['build_url']}
    number=build_number(job,proof['build_url'])
    if saved_number and saved_number!=number:
        raise NeedsAttention('回执对应 Build 与已保存编号不一致')
    proof.update(job_url=job,attempt_id=receipt['attempt_id'])
    patch_runtime(task_id,**{key+'_identity':proof,key+'_dispatch':{**receipt,'state':'ATTRIBUTED'}})
    return number
