#!/usr/bin/env python3
"""Thin local task client. Server-side approval rules remain authoritative."""
import argparse
import json
import urllib.request
import urllib.error
import uuid
from urllib.parse import urlparse
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app.paths import default_data_directory


def main():
    parser=argparse.ArgumentParser(description='图序本地任务入口，所有生成须先确认方案')
    parser.add_argument('--url',default='http://127.0.0.1:8766')
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('status');sub.add_parser('list')
    get=sub.add_parser('get');get.add_argument('task_id')
    paste=sub.add_parser('paste');paste.add_argument('file');paste.add_argument('--title',default='新文章');paste.add_argument('--genre',choices=['academic','news','novel','report'],default='report');paste.add_argument('--style',default='清晰克制的编辑插画，不生成文字')
    analyze=sub.add_parser('analyze');analyze.add_argument('task_id');analyze.add_argument('--model',default='qwen2.5vl:7b')
    director=sub.add_parser('director');director.add_argument('task_id');director.add_argument('--slot-id',required=True);director.add_argument('--config',required=True,help='UTF-8导演配置JSON文件；保存会使旧授权失效')
    approve=sub.add_parser('approve');approve.add_argument('task_id');approve.add_argument('--version',type=int,required=True);approve.add_argument('--checkpoint',default='');approve.add_argument('--provider',choices=['local','ark','openai','codex'],default='local');approve.add_argument('--quality',choices=['medium','high'],default='high');approve.add_argument('--max-attempts',type=int,default=6);approve.add_argument('--confirm',action='store_true',required=True)
    gen=sub.add_parser('generate');gen.add_argument('task_id');gen.add_argument('--version',type=int,required=True);gen.add_argument('--approval-id',required=True);gen.add_argument('--idempotency-key',default=None)
    cancel=sub.add_parser('cancel');cancel.add_argument('task_id')
    sub.add_parser('agent-requests')
    claim=sub.add_parser('agent-claim');claim.add_argument('task_id');claim.add_argument('--job-id',required=True);claim.add_argument('--slot-id',required=True);claim.add_argument('--approval-id',required=True)
    result=sub.add_parser('agent-result');result.add_argument('task_id');result.add_argument('--job-id',required=True);result.add_argument('--slot-id',required=True);result.add_argument('--approval-id',required=True);result.add_argument('--image',required=True)
    args=parser.parse_args()
    local_url = urlparse(args.url)
    if local_url.scheme != 'http' or local_url.hostname not in ('127.0.0.1','localhost') or local_url.username or local_url.password or local_url.path not in ('','/'):
        parser.error('此DEMO入口仅允许本机地址')
    token=''
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def request(method,path,payload=None):
        encoded=None if payload is None else json.dumps(payload,ensure_ascii=False).encode()
        req=urllib.request.Request(args.url.rstrip('/')+path,data=encoded,method=method,
            headers={'Content-Type':'application/json','X-Local-Token':token})
        try:
            with opener.open(req,timeout=600) as response:return json.load(response)
        except urllib.error.HTTPError as exc:
            raw=exc.read()
            try:message=json.loads(raw).get('detail',str(exc.code))
            except (ValueError,TypeError):message=f'本地服务返回HTTP {exc.code}'
            raise SystemExit(str(message))
        except urllib.error.URLError:
            raise SystemExit('无法连接图序本地服务，请先运行 ./start.sh 或“启动图序.command”。')
    boot=request('GET','/api/bootstrap')
    if boot.get('application', {}).get('id') != 'tuxu-community':
        raise SystemExit('该地址不是图序社区版，已拒绝操作；请检查端口。')
    token=boot['token']
    if args.command=='status':result={'models':boot['models'],'task_count':len(boot['tasks'])}
    elif args.command=='list':result=[{'id':t['id'],'title':t['title'],'status':t['status']} for t in boot['tasks']]
    elif args.command=='get':result=request('GET','/api/tasks/'+args.task_id)
    elif args.command=='paste':
        from pathlib import Path
        result=request('POST','/api/tasks/paste',{'title':args.title,'text':Path(args.file).read_text(encoding='utf-8'),'genre':args.genre,'style':args.style})
    elif args.command=='analyze':result=request('POST',f'/api/tasks/{args.task_id}/analyze',{'model':args.model})
    elif args.command=='director':
        from pathlib import Path
        current=request('GET',f'/api/tasks/{args.task_id}')
        plan=current.get('plan')
        if not plan:raise SystemExit('请先完成文章分析并保存配图方案。')
        if not any(slot['id']==args.slot_id for slot in plan['slots']):raise SystemExit('未找到当前方案的图位ID，请重新读取方案。')
        try:
            path=Path(args.config)
            if path.stat().st_size>16000:raise ValueError('配置文件过大')
            configuration=json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(configuration,dict):raise ValueError('配置必须是JSON对象')
        except (OSError,ValueError) as exc:raise SystemExit('导演配置读取失败：'+str(exc))
        slots=[dict(slot, director=configuration) if slot['id']==args.slot_id else slot for slot in plan['slots']]
        result=request('PUT',f'/api/tasks/{args.task_id}/plan',{'version':plan['version'],'style':plan['style'],'slots':slots})
    elif args.command=='approve':result=request('POST',f'/api/tasks/{args.task_id}/approve',{'version':args.version,'checkpoint':args.checkpoint,'image_provider':args.provider,'cloud_quality':args.quality,'max_attempts':args.max_attempts,'confirm':True})
    elif args.command=='agent-requests':result=request('GET','/api/agent-image-requests')
    elif args.command=='agent-claim':
        import os,sqlite3
        from pathlib import Path
        directory=default_data_directory()
        try:
            with sqlite3.connect('file:'+str(directory/'tasks.sqlite3')+'?mode=ro',uri=True) as local_db:
                local_token=local_db.execute("select value from settings where key='token'").fetchone()
            if not local_token or local_token[0] != token:raise ValueError
        except (sqlite3.Error,ValueError):
            raise SystemExit('本机数据目录与社区服务不匹配，未领取；请检查TUXU_DATA_DIR。')
        result=request('POST',f'/api/tasks/{args.task_id}/agent-image-claim',{'job_id':args.job_id,'slot_id':args.slot_id,'approval_id':args.approval_id})
        directory=default_data_directory()
        with sqlite3.connect('file:'+str(directory/'tasks.sqlite3')+'?mode=ro',uri=True) as connection:
            row=connection.execute('select data from tasks where id=?',(args.task_id,)).fetchone()
        if not row:raise SystemExit('协作请求已领取，但本机数据目录不匹配；不得重复调用，请核查原请求。')
        task=json.loads(row[0]);job=next((j for j in task['jobs'] if j['id']==args.job_id),None)
        if not job or job.get('approval_id')!=args.approval_id:raise SystemExit('本机协作快照不匹配；不得重复调用。')
        saved=directory/'agent-prompts';saved.mkdir(exist_ok=True,mode=0o700);saved.chmod(0o700)
        path=saved/(args.job_id+'-'+args.slot_id+'.json')
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as handle:json.dump({'request_id':result['request_id'],'prompt':job['prompts'][args.slot_id]['text']},handle,ensure_ascii=False)
        result['private_prompt_file']=str(path)
    elif args.command=='agent-result':
        import httpx
        from pathlib import Path
        path=Path(args.image)
        if not path.is_file() or path.stat().st_size>40*1024*1024:raise SystemExit('请选择不超过40MiB的实际生图文件。')
        with path.open('rb') as image, httpx.Client(trust_env=False,timeout=120) as client:
            response=client.post(args.url.rstrip('/')+f'/api/tasks/{args.task_id}/agent-image-result',headers={'X-Local-Token':token},data={'job_id':args.job_id,'slot_id':args.slot_id,'approval_id':args.approval_id},files={'image':(path.name,image,'application/octet-stream')})
        if response.status_code!=200:raise SystemExit('协作回填未通过，请核查原授权和请求；不重新生图。')
        result=response.json()
    elif args.command=='generate':
        current=request('GET',f'/api/tasks/{args.task_id}')
        jobs=[j for j in current['jobs'] if j['kind']=='generation' and j.get('approval_id')==args.approval_id and j.get('plan_version')==args.version]
        key=args.idempotency_key
        if key is None and jobs:
            pending=[j for j in jobs if any(s.get('state') in ('submitting','submitted','awaiting_agent') for s in j.get('submissions',[]))]
            latest=pending[-1] if pending else jobs[-1]
            saved_key=latest.get('idempotency_key')
            if not saved_key:raise SystemExit('历史作业缺少恢复键，请先核查原请求，不能自动重复提交。')
            unresolved=any(s.get('state') in ('submitting','submitted','awaiting_agent') for s in latest.get('submissions',[]))
            retryable=latest['status'] in ('failed','cancelled') and not unresolved and isinstance(latest.get('submissions'),list)
            key=None if retryable else saved_key
        result=request('POST',f'/api/tasks/{args.task_id}/generate',{'version':args.version,'approval_id':args.approval_id,'idempotency_key':key or uuid.uuid4().hex})
    else:result=request('POST',f'/api/tasks/{args.task_id}/cancel',{})
    if isinstance(result,dict) and 'document' in result:
        result={'id':result['id'],'title':result['title'],'status':result['status'],'plan':result.get('plan'),'approval_id':(result.get('approval') or {}).get('id')}
    elif isinstance(result,dict) and 'task' in result:
        result={k:v for k,v in result.items() if k!='task'}|{'task_id':result['task']['id'],'status':result['task']['status']}
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
