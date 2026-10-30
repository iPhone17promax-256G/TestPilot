<script setup lang="ts">
import { computed, ref, watch, defineAsyncComponent, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter, onBeforeRouteLeave, onBeforeRouteUpdate } from 'vue-router'
import AppIcon from '../components/AppIcon.vue'
import LoadStages from '../components/LoadStages.vue'
import {dateTime} from '../uiHelpers'
import FilesEditor from '../components/FilesEditor.vue'
import StepsEditor from '../components/StepsEditor.vue'
import StatusBadge from '../components/StatusBadge.vue'
import AppModal from '../components/AppModal.vue'
import {state,latestRun,saveRevision,startRun,createCase,notify,defaultSteps,request,refresh} from '../store'
import {kindLabels,type Kind,type StepMapping,type CaptureLevel} from '../types'
const CodeEditor=defineAsyncComponent(()=>import('../components/CodeEditor.vue'))
const route=useRoute(),router=useRouter(),query=ref(''),kind=ref('all')
const item=computed(()=>state.cases.find(c=>c.id===route.params.id))
const networkLevel=ref<CaptureLevel>('api')
const revision=ref(1),code=ref(''),environment=ref(''),saving=ref(false),tab=ref('code')
const steps=ref<Record<string,any>[]>([]),mode=ref('script'),showArchived=ref(false),stepsValid=ref(true)
const config=ref<Record<string,any>>({}),files=ref('{}'),stages=ref('[]'),stagesValid=ref(true)
const preview=ref<{code:string;stepMap:StepMapping[]}|null>(null),previewPending=ref(false),previewError=ref(''),sourceStep=ref('')
let previewTimer:ReturnType<typeof setTimeout>|undefined,previewTicket=0

const selected=computed(()=>item.value?.revisions.find(r=>r.number===revision.value))
function load(){stepsValid.value=true;stagesValid.value=true;mode.value=selected.value?.mode||'script';steps.value=JSON.parse(JSON.stringify(selected.value?.steps||[]));tab.value=mode.value==='steps'?'steps':'code';code.value=selected.value?.code??'';config.value=JSON.parse(JSON.stringify(selected.value?.config??{}));files.value=JSON.stringify(selected.value?.files??{},null,2);stages.value=JSON.stringify(config.value.stages??[],null,2);sourceStep.value=String(route.query.step||'');if(sourceStep.value)tab.value='code'}
const dirty=computed(()=>!!item.value&&(!stepsValid.value||mode.value!==(selected.value?.mode||'script')||JSON.stringify(steps.value)!==JSON.stringify(selected.value?.steps||[])||code.value!==selected.value?.code||JSON.stringify(config.value)!==JSON.stringify(selected.value?.config??{})||files.value!==JSON.stringify(selected.value?.files??{},null,2)||stages.value!==JSON.stringify(selected.value?.config?.stages??[],null,2)))
const visualDraft=computed(()=>mode.value==='steps'&&dirty.value)
const displayCode=computed(()=>visualDraft.value?(preview.value?.code||''):code.value)
const displayMap=computed(()=>visualDraft.value?(preview.value?.stepMap||[]):selected.value?.stepMap||[])
const highlighted=computed(()=>displayMap.value.find(s=>s.id===sourceStep.value))
function schedulePreview(){
  clearTimeout(previewTimer);const ticket=++previewTicket;preview.value=null;previewError.value=''
  if(mode.value!=='steps'||!item.value){previewPending.value=false;return}
  previewPending.value=true
  previewTimer=setTimeout(async()=>{try{const result=await request('/cases/preview',{kind:item.value!.kind,steps:steps.value});if(ticket===previewTicket)preview.value=result}catch(error){if(ticket===previewTicket)previewError.value=String(error)}finally{if(ticket===previewTicket)previewPending.value=false}},250)
}
watch([steps,mode,()=>item.value?.id],schedulePreview,{deep:true,flush:'sync'})
onUnmounted(()=>{clearTimeout(previewTimer);previewTicket++})
watch(()=>`${route.params.id}|${route.query.revision}|${!!item.value}`,()=>{if(!item.value)return;const n=Number(route.query.revision);revision.value=item.value.revisions.some(r=>r.number===n)?n:item.value.revisions.at(-1)!.number;load()},{immediate:true})
function canLeave(){return !dirty.value||window.confirm('当前用例尚未保存，确定放弃修改吗？')}
function protectReload(e:BeforeUnloadEvent){if(dirty.value){e.preventDefault();e.returnValue=''}}
onMounted(()=>window.addEventListener('beforeunload',protectReload));onUnmounted(()=>window.removeEventListener('beforeunload',protectReload))
onBeforeRouteLeave(canLeave);onBeforeRouteUpdate(canLeave)
const resultFilter=ref('all'),sort=ref('updated'),page=ref(1),pageSize=25
const filterKey='testpilot-cases-filters:'+state.projectId
try{const saved=JSON.parse(sessionStorage.getItem(filterKey)||'{}');query.value=saved.query||'';kind.value=saved.kind||'all';resultFilter.value=saved.result||'all';sort.value=saved.sort||'updated';showArchived.value=!!saved.archived;page.value=saved.page||1}catch{}
watch([query,kind,resultFilter,sort,showArchived],()=>{page.value=1;persistFilters()})
watch(page,persistFilters)
function persistFilters(){sessionStorage.setItem(filterKey,JSON.stringify({query:query.value,kind:kind.value,result:resultFilter.value,sort:sort.value,archived:showArchived.value,page:page.value}))}
const filtered=computed(()=>state.cases.filter(c=>(showArchived.value||!c.archived)&&(kind.value==='all'||c.kind===kind.value)&&(resultFilter.value==='all'||(latestRun(c.id)?.status||'draft')===resultFilter.value)&&`${c.name} ${c.id}`.toLowerCase().includes(query.value.toLowerCase())).sort((a,b)=>sort.value==='name'?a.name.localeCompare(b.name,'zh-CN'):new Date(b.revisions.at(-1)?.createdAt||0).getTime()-new Date(a.revisions.at(-1)?.createdAt||0).getTime()))
const pageCount=computed(()=>Math.max(1,Math.ceil(filtered.value.length/pageSize)))
watch(pageCount,count=>{page.value=Math.min(page.value,count)})
const paged=computed(()=>filtered.value.slice((page.value-1)*pageSize,page.value*pageSize))
const errorPercent=computed({get:()=>config.value.max_error_rate==null||config.value.max_error_rate===''?'':Number((Number(config.value.max_error_rate)*100).toFixed(6)),set:(value:number|string)=>{config.value.max_error_rate=value===''?null:Number(value)/100}})
const configError=computed(()=>{if(item.value?.kind!=='performance')return '';for(const [key,min,max] of [['users',1,100],['duration_seconds',1,1800],['ramp_seconds',0,1800],['think_ms',0,60000],['warmup_seconds',0,1800],['min_requests',1,100000000]] as const){const v=config.value[key];if(v!=null&&v!==''&&(!Number.isFinite(Number(v))||Number(v)<min||Number(v)>max))return '请检查用户数、时长与样本配置范围'}const rate=config.value.max_error_rate;if(rate!=null&&rate!==''&&(Number(rate)<0||Number(rate)>1))return '失败率应在 0%～100% 之间';return ''})
async function runListCase(id:string){const c=state.cases.find(c=>c.id===id);if(!c)return;try{router.push('/runs/'+await startRun(c,c.revisions.at(-1)!.number,state.environments[0]?.id||''))}catch(e){notify(String(e))}}

async function save(){if(!item.value||!stepsValid.value||!stagesValid.value||configError.value)return;saving.value=true;try{const options={...config.value};if(item.value.kind==='performance'){options.stages=JSON.parse(stages.value);for(const key of ['p95_ms','max_error_rate'])if(options[key]==='')options[key]=null;}await saveRevision(item.value.id,code.value,options,JSON.parse(files.value),mode.value==='steps'?steps.value:null);revision.value=item.value.revisions.at(-1)!.number;load()}catch(e){notify(String(e))}finally{saving.value=false}}
function changeRevision(e:Event){const select=e.target as HTMLSelectElement;if(!canLeave()){select.value=String(revision.value);return}revision.value=Number(select.value);load()}
async function run(smoke=false){if(!item.value||dirty.value)return notify('先保存修改，再运行固定版本');try{const overrides=smoke?{users:1,duration_seconds:3,ramp_seconds:1,warmup_seconds:0,stages:[]}:{};router.push('/runs/'+await startRun(item.value,revision.value,environment.value||state.environments[0]?.id||'',overrides,networkLevel.value))}catch{}}
function updateCode(value:string){if(mode.value==='script')code.value=value}
function downloadSource(){const url=URL.createObjectURL(new Blob([displayCode.value],{type:item.value?.kind==='performance'?'application/xml':'text/plain'}));const link=document.createElement('a');link.href=url;link.download=item.value?.filename??'test_case.py';link.click();URL.revokeObjectURL(url)}
async function importSource(e:Event){const file=(e.target as HTMLInputElement).files?.[0];if(file)code.value=await file.text()}
const createOpen=ref(false),form=ref({name:'',kind:'api' as Kind,description:'',mode:'steps'})
async function create(){if(!form.value.name.trim())return notify('填写用例名称');try{const id=await createCase(form.value.name.trim(),form.value.kind,form.value.description,form.value.mode);createOpen.value=false;router.push('/cases/'+id)}catch{}}
async function changeMode(e:Event){
  const input=e.target as HTMLSelectElement,next=input.value
  if(next===mode.value)return
  input.value=mode.value
  if(next==='steps'&&!window.confirm('将为新版本重新编排步骤。原脚本和历史版本保留，无法自动转换任意 Python 脚本。'))return
  if(next==='script'&&mode.value==='steps'){
    if(!stepsValid.value)return notify('先修正请求体格式')
    try{const result=await request('/cases/preview',{kind:item.value!.kind,steps:steps.value});code.value=result.code}catch(error){return notify('无法转换：'+String(error))}
  }
  mode.value=next
  if(next==='steps'&&!steps.value.length)steps.value=defaultSteps(item.value?.kind||'api')
  tab.value=next==='steps'?'steps':'code'
}

async function archive(){if(!item.value)return;await request('/cases/'+item.value.id+'/archive',{archived:!item.value.archived});await refresh(true);notify(item.value.archived?'用例已归档':'用例已恢复')}
</script>
<template><div class="page">
<template v-if="item">
<RouterLink to="/cases" class="back-link"><AppIcon name="back" :size="15"/>全部用例</RouterLink>
<div class="page-heading detail-heading"><div><div class="eyebrow">{{item.id}} · {{kindLabels[item.kind]}}</div><h1>{{item.name}}</h1><p>{{item.description}}</p></div><div class="heading-actions"><select v-model="environment" class="select environment-select" aria-label="执行环境"><option value="">默认环境</option><option v-for="e in state.environments" :key="e.id" :value="e.id">{{e.name}}</option></select><select v-if="item.kind==='web'" v-model="networkLevel" class="select compact" aria-label="运行时网络记录"><option value="api">网络：接口记录</option><option value="detailed">网络：详细记录</option><option value="off">网络：关闭</option></select><button class="button secondary" :disabled="!dirty||saving||!stepsValid||!stagesValid||!!configError||(mode==='steps'&&(previewPending||!!previewError))" @click="save">保存新版本</button><button v-if="item.kind==='performance'" class="button secondary" :disabled="dirty" @click="run(true)">单用户验证</button><button class="button primary" :disabled="dirty" @click="run()"><AppIcon name="play" :size="16"/>{{item.kind==='performance'?'启动压测':'运行测试'}}</button></div></div>
<div class="editor-layout"><section class="panel editor-panel">
<div class="editor-toolbar"><div><select class="select compact" aria-label="用例编辑模式" :value="mode" @change="changeMode"><option value="steps">可视化步骤</option><option value="script">原生脚本</option></select><span v-if="mode==='script'" class="mono">{{item.filename}}</span><span class="save-indicator" :class="{dirty}">{{dirty?'有未保存修改 · 保存后可运行':'已保存'}}</span></div><select class="select compact" aria-label="用例版本" :value="revision" @change="changeRevision"><option v-for="v in [...item.revisions].reverse()" :key="v.number" :value="v.number">v{{v.number}}</option></select></div>
<p v-if="selected?.fallback" class="steps-help">此版本采用原生脚本：{{selected.fallback.reason}}<span v-if="selected.fallback.user_quote"> · 用户要求：{{selected.fallback.user_quote}}</span></p>
<div class="local-tabs"><button v-if="mode==='steps'" :class="{active:tab==='steps'}" @click="tab='steps'">步骤编排</button><button :disabled="!stepsValid" :class="{active:tab==='code'}" @click="tab='code'">{{mode==='steps'?'对应源码':'脚本'}}</button><button :disabled="!stepsValid" v-if="item.kind==='performance'||item.kind==='app'" :class="{active:tab==='config'}" @click="tab='config'">运行配置</button><button :disabled="!stepsValid" :class="{active:tab==='files'}" @click="tab='files'">辅助文件</button></div>
<StepsEditor v-if="tab==='steps'" :key="item.id+revision" v-model="steps" :kind="item.kind" :source="displayCode" :mapping="displayMap" :preview-error="previewError" :preview-pending="previewPending" @validity="stepsValid=$event"/><template v-else-if="tab==='code'"><p v-if="mode==='steps'" class="steps-help">{{visualDraft?'当前草稿的实时源码预览；保存后形成固定版本。':'当前固定版本的执行源码；修改步骤后可实时预览新代码。'}}</p><p v-if="mode==='steps'&&previewError" class="step-preview-error" role="alert">{{previewError}}</p><p v-if="mode==='steps'&&visualDraft&&previewPending" class="steps-help">正在生成源码…</p><label v-if="mode==='steps'&&displayMap.length" class="source-step-nav field-label">定位步骤<select v-model="sourceStep" class="select"><option value="">选择对应代码段</option><option v-for="s in displayMap.filter(m=>m.start)" :key="s.id" :value="s.id">{{s.index}}. {{s.name}} · L{{s.start}}–{{s.end}}</option></select></label><CodeEditor :readonly="mode==='steps'" :model-value="displayCode" :highlight="highlighted?.start?{start:highlighted.start,end:highlighted.end!}:undefined" @update:model-value="updateCode" :language="item.kind==='performance'?'xml':'python'"/><div class="editor-foot"><span>每个版本保存源码、配置和辅助文件。</span><button class="text-button" :disabled="mode==='steps'&&(previewPending||!!previewError)" @click="downloadSource">下载源文件</button><label v-if="mode==='script'" class="text-button">导入 {{item.kind==='performance'?'JMX':'Python'}}<input type="file" :accept="item.kind==='performance'?'.jmx':'.py'" hidden @change="importSource"/></label></div></template>
<div v-else-if="tab==='config'" class="modal-body form-grid">
<template v-if="item.kind==='performance'">
<section class="config-section"><h3>固定负载</h3><div class="config-grid"><label class="field-label">模拟用户数<input v-model.number="config.users" type="number" min="1" max="100"/></label><label class="field-label">持续时间（秒）<input v-model.number="config.duration_seconds" type="number" min="1" max="1800"/></label><label class="field-label">增长时间（秒）<input v-model.number="config.ramp_seconds" type="number" min="0"/></label><label class="field-label">操作间隔（毫秒）<input v-model.number="config.think_ms" type="number" min="0"/></label></div><p class="form-hint">模拟用户数表示并发执行的用户，实际 RPS 取决于响应速度和操作间隔。</p></section>
<LoadStages v-model="stages" @validity="stagesValid=$event"/>
<section class="config-section"><h3>达标条件</h3><div class="config-grid"><label class="field-label">P95 上限（毫秒）<input v-model.number="config.p95_ms" type="number" min="1" placeholder="不设置"/></label><label class="field-label">最大失败率（%）<input v-model.number="errorPercent" type="number" min="0" max="100" step="0.1" placeholder="不设置"/></label><label class="field-label">最少有效样本<input v-model.number="config.min_requests" type="number" min="1"/></label></div><p class="form-hint">不设置耗时和失败率条件时，报告显示执行结果，不判定性能是否达标。</p></section><details class="advanced-section"><summary>高级设置</summary><label class="field-label">排除预热数据（秒）<input v-model.number="config.warmup_seconds" type="number" min="0"/></label></details><p v-if="configError" class="inline-error" role="alert">{{configError}}</p>
</template>
<template v-else><label class="field-label">应用版本<select v-model="config.app_id" class="select"><option value="">选择应用版本</option><option v-for="a in state.applications" :key="a.id" :value="a.id">{{a.name}} · {{a.version}}</option></select></label><label class="field-label">测试设备<select v-model="config.device_id" class="select"><option value="">选择设备</option><option v-for="d in state.devices" :key="d.id" :value="d.id">{{d.name}} · {{d.busy?'占用中':d.status}}</option></select></label><label class="field-label">应用状态<select v-model="config.reset" class="select"><option value="reset">重新初始化应用</option><option value="retain">保留应用数据</option></select></label><p class="form-hint">使用 driver fixture 与显式等待。正式执行会独占设备并保存截图、页面结构和日志。</p><RouterLink to="/mobile" class="text-button">管理应用与设备</RouterLink></template>
</div>
<FilesEditor v-else :model-value="JSON.parse(files)" @update:model-value="files=JSON.stringify($event,null,2)"/>
</section></div><details class="case-details"><summary>用例信息与历史结果</summary><dl class="metadata-list"><div><dt>编号</dt><dd class="mono">{{item.id}}</dd></div><div><dt>来源</dt><dd>{{item.source}}</dd></div><div><dt>当前版本</dt><dd>v{{revision}}</dd></div><div><dt>本版本结果</dt><dd><StatusBadge :status="latestRun(item.id,revision)?.status??'draft'"/></dd></div></dl><div class="heading-actions"><RouterLink v-if="latestRun(item.id,revision)" :to="'/runs/'+latestRun(item.id,revision)?.id" class="button secondary">查看 v{{revision}} 报告</RouterLink><button class="text-button" @click="archive">{{item.archived?'恢复用例':'归档用例'}}</button></div></details>
</template>
<div v-else-if="route.params.id" class="panel empty-state"><h3>找不到这个用例</h3><RouterLink to="/cases">返回列表</RouterLink></div>
<template v-else><div class="page-heading"><div><h1>测试用例 <span class="heading-count">{{state.cases.filter(c=>!c.archived).length}}</span></h1><p>编写可视化步骤、维护版本，并在目标环境中验证。</p></div><button class="button primary" @click="createOpen=true"><AppIcon name="plus" :size="16"/>新建用例</button></div><div class="list-toolbar"><div class="filter-tabs"><button v-for="k in ['all','api','web','performance','app']" :key="k" :class="{active:kind===k}" :aria-pressed="kind===k" @click="kind=k">{{k==='all'?'全部':kindLabels[k as Kind]}}</button></div><label class="search-field"><AppIcon name="search" :size="16"/><input v-model="query" aria-label="搜索用例" placeholder="搜索名称或编号"/></label><select v-model="resultFilter" class="select" aria-label="筛选用例结果"><option value="all">全部结果</option><option value="passed">通过</option><option value="failed">断言失败</option><option value="error">执行异常</option><option value="draft">未运行</option><option value="running">运行中</option><option value="completed">已完成</option><option value="timed_out">超时</option><option value="cancelled">已取消</option><option value="interrupted">已中断</option><option value="queued">排队中</option></select><select v-model="sort" class="select" aria-label="用例排序"><option value="updated">最近更新</option><option value="name">名称排序</option></select><label class="archive-toggle"><input v-model="showArchived" type="checkbox"/>已归档</label></div><div class="panel table-panel"><table class="data-table cases-table"><thead><tr><th>用例</th><th>类型</th><th>当前版本结果</th><th>最近更新</th><th>操作</th></tr></thead><tbody><tr v-for="c in paged" :key="c.id"><td><RouterLink :to="'/cases/'+c.id" class="table-case-title">{{c.name}}</RouterLink><div class="table-secondary">{{c.revisions.at(-1)?.mode==='steps'?'可视化步骤':'原生脚本'}}<span v-if="c.archived"> · 已归档</span></div></td><td><span class="kind-label" :class="c.kind">{{kindLabels[c.kind]}}</span></td><td><StatusBadge :status="latestRun(c.id)?.status??'draft'"/><span class="result-version">v{{c.revisions.at(-1)?.number}}</span></td><td class="muted">{{dateTime(c.revisions.at(-1)?.createdAt)}}</td><td><div class="row-actions"><RouterLink :to="'/cases/'+c.id" class="text-button">编辑</RouterLink><button v-if="c.kind!=='performance'" class="text-button" :disabled="!!c.archived" @click="runListCase(c.id)">运行</button><RouterLink v-else :to="'/cases/'+c.id" class="text-button">压测配置</RouterLink></div></td></tr></tbody></table><div v-if="!filtered.length" class="empty-state"><AppIcon name="search" :size="25"/><h3>没有匹配的用例</h3><button class="text-button" @click="query='';kind='all';resultFilter='all'">清除筛选</button></div><div class="table-footer"><span>{{filtered.length}} 条 · 每页 {{pageSize}} 条</span><div class="pagination"><button class="icon-button" aria-label="上一页用例" :disabled="page<=1" @click="page--"><AppIcon name="back" :size="16"/></button><span>{{page}} / {{pageCount}}</span><button class="icon-button" aria-label="下一页用例" :disabled="page>=pageCount" @click="page++"><AppIcon name="arrow" :size="16"/></button></div></div></div></template>
<AppModal :open="createOpen" title="新建测试用例" @close="createOpen=false"><form @submit.prevent="create"><div class="modal-body form-grid"><label class="field-label">用例名称<input v-model="form.name" required maxlength="120"/></label><label class="field-label">测试类型<select v-model="form.kind" class="select"><option v-for="(label,k) in kindLabels" :key="k" :value="k">{{label}}</option></select></label><label class="field-label">编写方式<select v-model="form.mode" class="select"><option value="steps">可视化步骤（推荐）</option><option value="script">原生脚本</option></select></label><label class="field-label">目标与预期<textarea v-model="form.description" rows="3"/></label><p class="form-hint">创建后补充请求、操作和断言。性能用例可以导入原生 JMX。</p></div><div class="modal-actions"><button class="button primary" type="submit">创建并编辑</button></div></form></AppModal>
</div></template>
