import { reactive } from 'vue'
import {setClientAccount, configureRuntime, modelSelection, localClient} from './localClient'
import type { AgentTask, Run, TestCase, Environment, Application, Device, ChatAttachment, CaptureLevel, ProjectSource, PermissionMode } from './types'
interface Data {applications:Application[];devices:Device[];cases:TestCase[];runs:Run[];tasks:AgentTask[];environments:Environment[];sources:ProjectSource[];settings:{model:string;baseUrl:string;maxSteps:number;configured:boolean};projectId:string;projects:{id:string;name:string;description:string;ownerId?:string;members?:string[]}[];projectName:string;projectDescription:string}
export const state=reactive<Data>({applications:[],devices:[],cases:[],runs:[],tasks:[],environments:[],sources:[],settings:{model:'ark-code-latest',baseUrl:'',maxSteps:24,configured:false},projectId:localStorage.getItem('testpilot-project')||'PROJECT',projects:[],projectName:'',projectDescription:''})
export const ui=reactive({toast:'',error:'',loading:true,connected:false,authRequired:true,initialized:false,setupRequired:false,deploymentMode:'server',managedExecutor:false,user:null as {id:string;username:string;role:string}|null})
let timer:ReturnType<typeof setTimeout>
const snapshots=new Map<string,{etag:string;data:Data}>()
export function notify(message:string){ui.toast=message.replace(/^Error:\s*/,'');clearTimeout(timer);timer=setTimeout(()=>ui.toast='',6000)}
export async function request<T=any>(path:string,body?:unknown):Promise<T>{
 const headers:Record<string,string>={'X-TestPilot-Project':state.projectId}
 if(body!==undefined)headers['Content-Type']='application/json'
 if(path==='/state'&&snapshots.has(state.projectId))headers['If-None-Match']=snapshots.get(state.projectId)!.etag
 const response=await fetch('/api'+path,{method:body===undefined?'GET':'POST',headers,body:body===undefined?undefined:JSON.stringify(body)})
 if(response.status===304)return snapshots.get(state.projectId)!.data as T
 const data=await response.json()
 if(response.ok&&path==='/state')snapshots.set(state.projectId,{etag:response.headers.get('ETag')||'',data})
 if(response.status===401&&!path.startsWith('/auth/')){ui.authRequired=true;ui.initialized=false}
 if(!response.ok)throw new Error(data.message||data.detail||`HTTP ${response.status}`)
 return data
}
export async function uploadChatAttachment(file:File):Promise<ChatAttachment>{
 if(file.size>10*1024*1024)throw new Error('单个文件最多 10 MB')
 const response=await fetch('/api/attachments?filename='+encodeURIComponent(file.name),{method:'POST',headers:{'X-TestPilot-Project':state.projectId,'Content-Type':'application/octet-stream'},body:file})
 const data=await response.json()
 if(response.status===401){ui.authRequired=true;ui.initialized=false}
 if(!response.ok)throw new Error(data.message||data.detail||`HTTP ${response.status}`)
 return data
}
export async function uploadProjectSource(file:File,projectId=state.projectId):Promise<ProjectSource>{
 if(!file.size)throw new Error('文件不能为空')
 if(file.size>10*1024*1024)throw new Error('单个文件最多 10 MB')
 const response=await fetch('/api/sources/upload?filename='+encodeURIComponent(file.name),{method:'POST',headers:{'X-TestPilot-Project':projectId,'Content-Type':'application/octet-stream'},body:file})
 const data=await response.json()
 if(response.status===401){ui.authRequired=true;ui.initialized=false}
 if(!response.ok)throw new Error(data.message||data.detail||`HTTP ${response.status}`)
 return data
}
let refreshPromise:Promise<void>|undefined
export async function refresh(force=false):Promise<void>{
 if(refreshPromise){await refreshPromise;if(force)return refresh();return}
 refreshPromise=(async()=>{try{Object.assign(state,await request<Data>('/state'));ui.connected=true;ui.initialized=true;ui.error=''}catch(error){ui.connected=false;ui.error=String(error)}finally{ui.loading=false}})()
 try{await refreshPromise}finally{refreshPromise=undefined}
}
export async function initialize(){try{const auth=await request('/auth/me');ui.deploymentMode=auth.deploymentMode||'server';ui.managedExecutor=auth.managedExecutor===true;configureRuntime(ui.deploymentMode,ui.managedExecutor,auth.user?.role==='admin');state.projectId=localStorage.getItem('testpilot-project:'+auth.user?.id)||'PROJECT';ui.user=auth.user;setClientAccount(auth.user?.id||'');ui.setupRequired=auth.setupRequired;ui.authRequired=!auth.user;if(auth.user)await refresh()}catch(error){ui.error=String(error)}finally{ui.loading=false}}
export async function login(username:string,password:string){const result=await request(ui.setupRequired?'/auth/setup':'/auth/login',{username,password});ui.user=result.user;state.projectId=localStorage.getItem('testpilot-project:'+result.user.id)||'PROJECT';configureRuntime(ui.deploymentMode,ui.managedExecutor,result.user.role==='admin');setClientAccount(result.user.id);snapshots.clear();ui.authRequired=false;ui.setupRequired=false;await refresh(true)}
export async function logout(){await request('/auth/logout',{});ui.user=null;setClientAccount('');ui.authRequired=true;ui.initialized=false;snapshots.clear();state.tasks=[];state.cases=[];state.runs=[]}
export async function switchProject(id:string){if(refreshPromise)await refreshPromise;state.projectId=id;localStorage.setItem('testpilot-project:'+ui.user?.id,id);ui.loading=true;ui.initialized=false;Object.assign(state,{cases:[],runs:[],tasks:[],environments:[],sources:[],applications:[]});await refresh(true)}
async function action<T>(fn:()=>Promise<T>):Promise<T>{try{return await fn()}catch(error){notify(String(error));throw error}}
export function latestRun(id:string,revision?:number){const number=revision??state.cases.find(c=>c.id===id)?.revisions.at(-1)?.number;return state.runs.find(r=>r.caseId===id&&r.revision===number)}
export function caseName(id:string){return state.cases.find(c=>c.id===id)?.name??id}
export async function saveRevision(id:string,code:string,config:Record<string,any>={},files:Record<string,string>={},steps:Record<string,any>[]|null=null){const item=state.cases.find(c=>c.id===id)!;const result=await action(()=>request('/tools/cases_save_revision',{arguments:{name:item.name,kind:item.kind,description:item.description,code,steps,case_id:id,entry:item.revisions.at(-1)?.entry??'test_case.py',execution_config:config,files},key:crypto.randomUUID()}));await refresh(true);notify('版本已保存，历史报告保留');return result}
export function defaultSteps(kind:string):Record<string,any>[]{
 if(kind==='api')return [{id:crypto.randomUUID(),name:'发送请求',action:'request',method:'GET',path:'/',enabled:true},{id:crypto.randomUUID(),name:'检查 HTTP 状态',action:'assert',source:'status',operator:'eq',expected:200,enabled:true}]
 if(kind==='performance')return [{id:crypto.randomUUID(),name:'HTTP 请求',action:'request',method:'GET',path:'/',expected_status:200,enabled:true}]
 if(kind==='web')return [{id:crypto.randomUUID(),name:'打开页面',action:'goto',path:'/',enabled:true}]
 return [{id:crypto.randomUUID(),name:'占位：添加实际操作后删除此步',action:'python',python:'import pytest\npytest.skip("请添加实际 APP 操作和业务断言")',enabled:true}]
}
export async function createCase(name:string,kind:string,description:string,mode='steps'){const argumentsValue:Record<string,any>={name,kind,description};if(mode==='steps')argumentsValue.steps=defaultSteps(kind);else{argumentsValue.code='import pytest\n\ndef test_example():\n    pytest.skip("请补充实际操作和业务断言")\n';if(kind==='performance'){const plan=await request('/tools/performance_plan',{arguments:{requests:[{name:'请补充目标接口',path:'/'}]}});argumentsValue.code=plan.code}}const result=await action(()=>request<TestCase>('/tools/cases_save_revision',{arguments:argumentsValue,key:crypto.randomUUID()}));await refresh(true);return result.id}
export async function startRun(item:TestCase,revision:number,environmentId:string,overrides:Record<string,any>={},networkCapture:CaptureLevel='api'){if(!environmentId)throw new Error('请先添加运行环境');const result=await action(()=>request<Run>('/runs',{case_id:item.id,revision_number:revision,environment_id:environmentId,overrides,network_capture:item.kind==='web'?networkCapture:null,key:crypto.randomUUID()}));await refresh(true);notify('已提交测试，等待 Runner 领取');return result.id}
export async function startTask(goal:string,environmentId:string,attachmentIds:string[]=[],networkCapture:CaptureLevel|null=null,permissionMode:PermissionMode='auto'){if(!environmentId)throw new Error('请先添加运行环境');const result=await action(()=>request<AgentTask>('/tasks',{goal,environment_id:environmentId,attachment_ids:attachmentIds,network_capture:networkCapture,permission_mode:permissionMode,...modelSelection(),key:crypto.randomUUID()}));await refresh(true);return result.id}
export async function sendMessage(id:string,content:string,attachmentIds:string[]=[],networkCapture:CaptureLevel|null=null){await action(()=>request('/tasks/'+id+'/messages',{content,attachment_ids:attachmentIds,network_capture:networkCapture,model_profile_id:modelSelection().model_profile_id,reasoning_effort:localClient.effort,key:crypto.randomUUID()}));await refresh(true)}
export async function setTaskPermission(id:string,mode:PermissionMode){await action(()=>request('/tasks/'+id+'/permissions',{mode}));await refresh(true)}
export async function decideApproval(id:string,approvalId:string,decision:'approve'|'reject',reason=''){await action(()=>request('/tasks/'+id+'/approvals/'+approvalId,{decision,reason}));await refresh(true)}
export async function cancelTask(id:string){await action(()=>request('/tasks/'+id+'/cancel',{}));await refresh(true);notify('已停止，之后仍可继续对话')}
export async function resumeTask(id:string){await action(()=>request('/tasks/'+id+'/resume',{}));await refresh()}
export async function cancelRun(id:string){await action(()=>request('/runs/'+id+'/cancel',{}));await refresh()}
export async function saveEnvironment(body:unknown){await action(()=>request('/environments',body));await refresh(true);notify('环境已保存')}
export async function saveSettings(body:unknown){await action(()=>request('/settings',body));await refresh(true);notify('模型配置已保存')}
void initialize()
setInterval(()=>{if(document.visibilityState==='visible'&&!ui.authRequired)void refresh()},2500)
