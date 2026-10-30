from pathlib import Path
import json,urllib.request,time
from testpilot.config import settings
from https_client import require_https_url,open_https
account,binding=next(iter(json.loads(Path('/client/bindings.json').read_text()).items()))
# 功能：为手动配对执行器演示验收发送 HTTP 请求。
# 输入：path 是完整接口路径，data 是可选 JSON，local=True 请求 8021 执行器否则请求平台；binding 从脚本的配对文件读取。
# 输出/影响：按目标选择本地或平台 token 并返回 JSON；调用写接口会创建演示用例、Run/Task，不把 token 打印出来。
def req(path,data=None,local=False):
 base=require_https_url('https://127.0.0.1:8021' if local else settings.platform_url,'执行器地址' if local else '平台地址')
 headers={'Authorization':'Bearer '+binding['localToken' if local else 'platformToken'],'Content-Type':'application/json','X-TestPilot-Project':'PROJECT'}
 raw=json.dumps(data).encode() if data is not None else None
 with open_https(urllib.request.Request(base+path,data=raw,headers=headers),timeout=65) as r:return json.load(r)
status=req('/status',local=True);state=req('/api/state');env=state['environments'][0]['id'];models=req('/api/models');model=next((p for p in models if p.get('default')),models[0])
print('Connected executor:',status['executorId'],flush=True)
case=req('/api/tools/cases_save_revision',{'arguments':{'name':'本地执行器验收 · MM ping','kind':'api','description':'验证任务领取、用例文件下载和报告回传','steps':[{'action':'request','name':'请求 ping','method':'GET','path':'/api/v4/system/ping'},{'action':'assert','name':'HTTP 为 200','source':'status','operator':'eq','expected':200},{'action':'assert','name':'业务状态为 OK','source':'jmespath','expression':'status','operator':'eq','expected':'OK'}]}})
run=req('/api/runs',{'case_id':case['id'],'revision_number':1,'environment_id':env,'timeout':30})
task=req('/api/tasks',{'goal':'只读检查：调用 environment_status 查看本机能否运行接口测试，然后用一句话说明。不要创建用例或启动测试。','environment_id':env,'model_profile_id':model['id'],'executor_id':status['executorId'],'permission_mode':'readonly'})
Path('/client/acceptance.json').write_text(json.dumps({'taskId':task['id'],'runId':run['id'],'caseId':case['id'],'modelId':model['id'],'environmentId':env},ensure_ascii=False))
print('Started:',task['id'],run['id'],flush=True)
for _ in range(25):
 state=req('/api/state');t=next(v for v in state['tasks'] if v['id']==task['id']);r=next(v for v in state['runs'] if v['id']==run['id'])
 print('Agent:',t['status'],t.get('errorType'),t.get('errorLocations',[]),'Run:',r['status'],r.get('summary','')[:100],flush=True)
 if t['status'] not in {'queued','running'} and r['status'] not in {'queued','running'}:break
 time.sleep(2)
