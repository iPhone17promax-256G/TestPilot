#!/usr/bin/env python3
"""成品包验收：MM 接口/Web/JMeter 真执行；Agent 用受控模型验证两次工具调用及继续对话。

仅用于 releases/acceptance-local、acceptance-server 这两个独立安装，不修改日常数据库。
模型替身不访问供应商、不消耗真实 Key，不能据此声称验证了某个真实 LLM 的能力。
"""
import json
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
MODEL_PROGRAM='''import json
from http.server import HTTPServer,BaseHTTPRequestHandler
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_POST(self):
  body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
  count=sum(m['role']=='tool' for m in body['messages'])
  name=['project_context','environment_status'][count] if count<2 else None
  message={'role':'assistant','content':'部署验收：已读取项目资料并检查执行环境，测试在独立执行端进行。'}
  if name:message={'role':'assistant','content':None,'tool_calls':[{'id':'call-check-'+str(count),'type':'function','function':{'name':name,'arguments':'{}'}}]}
  result={'id':'chatcmpl-check','object':'chat.completion','created':1,'model':'deployment-check','choices':[{'index':0,'message':message,'finish_reason':'tool_calls' if name else 'stop'}],'usage':{'prompt_tokens':10,'completion_tokens':10,'total_tokens':20}}
  if body.get('stream'):
   delta=dict(message)
   if name:delta['tool_calls']=[dict(call,index=i) for i,call in enumerate(message['tool_calls'])]
   chunk=dict(result,object='chat.completion.chunk',choices=[{'index':0,'delta':delta,'finish_reason':None}])
   final=dict(result,object='chat.completion.chunk',choices=[{'index':0,'delta':{},'finish_reason':'tool_calls' if name else 'stop'}])
   data=('data: '+json.dumps(chunk)+'\\n\\ndata: '+json.dumps(final)+'\\n\\ndata: [DONE]\\n\\n').encode();mime='text/event-stream'
  else:data=json.dumps(result).encode();mime='application/json'
  self.send_response(200);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
HTTPServer(('0.0.0.0',8089),Handler).serve_forever()
'''


def docker(*args):return subprocess.run(['docker',*args],check=True,stdout=subprocess.DEVNULL)


def verify(mode,port):
    base=f'http://127.0.0.1:{port}'
    cookies=urllib.request.HTTPCookieProcessor(__import__('http.cookiejar',fromlist=['CookieJar']).CookieJar())
    client=urllib.request.build_opener(cookies)
    def req(path,body=None):
        raw=json.dumps(body).encode() if body is not None else None
        with client.open(urllib.request.Request(base+'/api'+path,data=raw,headers={'Content-Type':'application/json','X-AIQA-Project':'PROJECT'}),timeout=75) as response:return json.load(response)
    me=req('/auth/me');assert me['deploymentMode']==mode
    if mode=='server':
        if me['setupRequired']:req('/auth/setup',dict(username='acceptance_admin',password='DisposableDemo2026!'))
        else:req('/auth/login',dict(username='acceptance_admin',password='DisposableDemo2026!'))
    else:assert me['user']['id']=='LOCAL'
    state=req('/state');environment=state['environments'][0]['id']
    for _ in range(15):
        try:
            status=req('/runtime/status')
            if status['platform'].get('online'):break
        except Exception:pass
        time.sleep(1)
    assert status['platform']['online']
    probe=req('/runtime/environment',{'smoke':True})
    assert any(row['id']=='web' and row['status']=='ready' and '自检通过' in row['detail'] for row in probe['checks'])
    requests=[{'action':'request','path':'/api/v4/system/ping','expected_status':200,'assert_json':{'$.status':'OK'}}]
    specs=[('api',requests+[{'action':'assert','source':'status','expected':200},{'action':'assert','source':'json','expression':'status','expected':'OK'}],{}),
      ('web',[{'action':'goto','path':'/login'},
         {'action':'wait','selector':"#input_loginId, a[href='http://mm-target:8065/login']",'timeout':30000},
         {'action':'if','source':'element','by':'role','role':'link','selector':'View in Browser','timeout':5000},
         {'action':'click','by':'role','role':'link','selector':'View in Browser'},{'action':'end'},
         {'action':'assert_visible','selector':'#input_loginId'},{'action':'assert_visible','selector':'#input_password-input'},
         {'action':'fill','selector':'#input_loginId','value':'aiqa-deployment-check'},
         {'action':'assert_value','selector':'#input_loginId','value':'aiqa-deployment-check'},
         {'action':'assert_enabled','selector':'#saveSetting'}],{}),
      ('performance',requests,dict(users=1,duration_seconds=3,ramp_seconds=0,p95_ms=1000,max_error_rate=0))]
    run_ids=[]
    for kind,steps,config in specs:
        case=req('/tools/cases_save_revision',{'arguments':dict(name=f'双模式验收 · {mode} · {kind}',kind=kind,steps=steps,execution_config=config)})
        run=req('/runs',dict(case_id=case['id'],revision_number=1,environment_id=environment,timeout=90))
        run_ids.append(run['id'])
    model=req('/runtime/models',{'profile':dict(name='受控模型 · 部署验收',provider='compatible',model='deployment-check',baseUrl='http://model-probe:8089/v1',default=True,freeOnly=False),'apiKey':'acceptance-only-not-a-provider-key'})
    task=req('/tasks',dict(goal='只读查看项目资料和运行环境，不保存用例或执行测试。',environment_id=environment,model_profile_id=model['id'],executor_id=status['executorId'],permission_mode='readonly'))
    for _ in range(90):
        state=req('/state');runs=[row for row in state['runs'] if row['id'] in run_ids];conversation=next(row for row in state['tasks'] if row['id']==task['id'])
        if len(runs)==3 and all(row['status'] not in {'queued','running'} for row in runs) and conversation['status'] not in {'queued','running'}:break
        time.sleep(1)
    for row in runs:
        print(mode,'真实执行',row['kind'] if 'kind' in row else row.get('caseId'),row['status'],row['summary'],flush=True)
        assert row['status']=='passed',(row['id'],row['summary'],row.get('logs','')[-1500:])
    assert len(conversation['events'])>=2,conversation
    assert conversation['status']=='completed',conversation
    req('/tasks/'+task['id']+'/messages',dict(content='继续，用一句话总结。',model_profile_id=model['id']))
    for _ in range(30):
        conversation=next(row for row in req('/state')['tasks'] if row['id']==task['id'])
        if conversation['status']=='completed' and len([m for m in conversation['messages'] if m['role']=='user'])==2:break
        time.sleep(1)
    assert conversation['status']=='completed' and all(m.get('delivered') for m in conversation['messages'] if m['role']=='user')
    print(mode,'Agent 连续工具调用与后续消息通过；没有真实供应商调用',flush=True)
    return dict(mode=mode,caseRuns=run_ids,taskId=task['id'],environment=probe)


def main():
    path=ROOT/'.client/acceptance-model.py';path.write_text(MODEL_PROGRAM)
    docker('run','-d','--name','aiqa-deployment-model-check','--network','aiqa-local-check_entry','--network-alias','model-probe','-v',str(path)+':/probe.py:ro','aiqa-runtime:0.2','python','/probe.py')
    docker('network','connect','--alias','model-probe','aiqa-server-check_entry','aiqa-deployment-model-check')
    for name in ['aiqa-local-check','aiqa-server-check']:
        networks=json.loads(subprocess.check_output(['docker','inspect','-f','{{json .NetworkSettings.Networks}}','mm-test-app'],text=True))
        if name+'-targets' not in networks:docker('network','connect','--alias','mm-target',name+'-targets','mm-test-app')
    try:
        rows=[verify('local',8032),verify('server',8033)]
        (ROOT/'.client/deployment-acceptance.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2))
    finally:
        docker('rm','-f','aiqa-deployment-model-check')
    print('双模式成品包验收通过。')


if __name__=='__main__':main()
