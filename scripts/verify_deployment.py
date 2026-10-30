#!/usr/bin/env python3
"""成品包验收：MM 接口/Web/JMeter 真执行；Agent 用受控模型验证两次工具调用及继续对话。

仅用于 releases/acceptance-local、acceptance-server 这两个独立安装，不修改日常数据库。
模型替身不访问供应商、不消耗真实 Key，不能据此声称验证了某个真实 LLM 的能力。
"""
import json
import ssl
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


# 功能：在双模式验收脚本中执行 Docker CLI。
# 输入：args 是逐项命令参数，由本脚本固定验收步骤构造。
# 输出/影响：返回 CompletedProcess，失败抛异常；执行的命令可能创建/连网/删除验收容器，标准输出被丢弃。
def docker(*args):return subprocess.run(['docker',*args],check=True,stdout=subprocess.DEVNULL)


# 功能：验收独立安装的本地或服务端模式。
# 输入：mode 是 local/server，port 是该验收安装的 HTTPS 端口。
# 输出/影响：实际保存并执行 MM API/Web/JMeter 用例，使用受控模型验证 Agent 多轮与续聊，返回运行 ID/任务 ID/环境报告；不能据此声称验证真实模型能力。
def verify(mode,port):
    base=f'https://127.0.0.1:{port}'
    # 两个独立安装各有自己的本地 CA；必须验证对应证书，不能为验收关闭 TLS 校验。
    ca_bundle=ROOT/'releases'/('acceptance-'+mode)/'tls/ca-bundle.pem'
    context=ssl.create_default_context(cafile=str(ca_bundle))
    cookies=urllib.request.HTTPCookieProcessor(__import__('http.cookiejar',fromlist=['CookieJar']).CookieJar())
    client=urllib.request.build_opener(cookies,urllib.request.HTTPSHandler(context=context))
    # 功能：在单次部署验收中发送带 Cookie 的平台请求。
    # 输入：path 是 API 子路径，body 是可选 JSON；base/client 由外层 verify 闭包提供。
    # 输出/影响：返回解析的 JSON；登录 Cookie 随 client 保存复用，写接口会产生该验收安装的记录。
    def req(path,body=None):
        raw=json.dumps(body).encode() if body is not None else None
        with client.open(urllib.request.Request(base+'/api'+path,data=raw,headers={'Content-Type':'application/json','X-TestPilot-Project':'PROJECT'}),timeout=75) as response:return json.load(response)
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
         {'action':'fill','selector':'#input_loginId','value':'testpilot-deployment-check'},
         {'action':'assert_value','selector':'#input_loginId','value':'testpilot-deployment-check'},
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


# 功能：组织两套独立安装的自动验收与清理。
# 输入：无参数；ROOT 和受控模型程序来自脚本全局配置，目标是固定验收端口。
# 输出/影响：写临时模型脚本/验收结果 JSON，创建并最终删除模型替身容器，连接验收网络；不调用真实供应商。
def main():
    path=ROOT/'.client/acceptance-model.py';path.write_text(MODEL_PROGRAM)
    docker('run','-d','--name','testpilot-deployment-model-check','--network','testpilot-local-check_entry','--network-alias','model-probe','-v',str(path)+':/probe.py:ro','testpilot-runtime:0.2','python','/probe.py')
    docker('network','connect','--alias','model-probe','testpilot-server-check_entry','testpilot-deployment-model-check')
    for name in ['testpilot-local-check','testpilot-server-check']:
        networks=json.loads(subprocess.check_output(['docker','inspect','-f','{{json .NetworkSettings.Networks}}','mm-test-app'],text=True))
        if name+'-targets' not in networks:docker('network','connect','--alias','mm-target',name+'-targets','mm-test-app')
    try:
        rows=[verify('local',8032),verify('server',8033)]
        (ROOT/'.client/deployment-acceptance.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2))
    finally:
        docker('rm','-f','testpilot-deployment-model-check')
    print('双模式成品包验收通过。')


if __name__=='__main__':main()
