<script setup lang="ts">
import {computed,onMounted,onUnmounted,ref,watch} from 'vue'
import AppModal from './AppModal.vue'
import {state,request,notify} from '../store'
import {clientRequest,localClient,refreshClient} from '../localClient'
const emit=defineEmits<{read:[id:string,offset:number]}>()
const info=ref<any>(null),settingsOpen=ref(false),working=ref(false),error=ref(''),key=ref(''),reuse=ref('')
const query=ref(''),mode=ref('auto'),results=ref<any[]>([]),searching=ref(false),warning=ref(''),usedMode=ref('')
const form=ref({baseUrl:'https://ark.cn-beijing.volces.com/api/plan/v3',model:'doubao-embedding-vision',chunkSize:1200,overlap:150,maxCharacters:200000})
const running=computed(()=>['queued','running'].includes(info.value?.job?.status))
const compatibleModels=computed(()=>localClient.models.filter(m=>m.hasKey&&m.baseUrl.replace(/\/$/,'')===form.value.baseUrl))
let timer:ReturnType<typeof setInterval>|undefined,lastLoaded=0
async function load(){const project=state.projectId;try{const data=await request('/knowledge');if(project===state.projectId){info.value=data;error.value='';lastLoaded=Date.now()}}catch(e){if(project===state.projectId)error.value=String(e)}}
async function configure(){
 settingsOpen.value=true;key.value='';reuse.value='';const cfg=info.value?.config
 form.value={baseUrl:cfg?.baseUrl||'https://ark.cn-beijing.volces.com/api/plan/v3',model:cfg?.model||'doubao-embedding-vision',chunkSize:cfg?.chunkSize||1200,overlap:cfg?.overlap??150,maxCharacters:cfg?.maxCharacters||200000}
 await refreshClient(true)
 try{const project=state.projectId;const data=await clientRequest('/knowledge?projectId='+encodeURIComponent(project));if(project===state.projectId)info.value={...info.value,...data}}catch(e){notify(String(e))}
}
async function save(){const project=state.projectId;working.value=true;try{await clientRequest('/knowledge',{projectId:project,config:form.value,apiKey:key.value,reuseModelId:reuse.value});key.value='';if(project===state.projectId){await load();notify('RAG 配置已保存，尚未调用向量模型');settingsOpen.value=false}}catch(e){notify(String(e))}finally{working.value=false}}
async function test(){working.value=true;const project=state.projectId;try{const data=await clientRequest('/knowledge/test',{projectId:project,text:'项目资料语义检索连接检测'});if(project===state.projectId)notify(`向量模型可用：${data.dimension} 维，本次用量 ${data.tokens} token`)}catch(e){notify(String(e))}finally{working.value=false}}
async function index(){if(!info.value?.config)return configure();const project=state.projectId;working.value=true;try{await request('/knowledge/index',{});if(project===state.projectId){await load();notify('索引任务已提交，仅处理当前项目资料')}}catch(e){notify(String(e))}finally{working.value=false}}
async function cancel(){working.value=true;try{await request('/knowledge/cancel',{});await load()}catch(e){notify(String(e))}finally{working.value=false}}
async function search(){const project=state.projectId;searching.value=true;warning.value='';try{const data=await request('/knowledge/search',{query:query.value,mode:mode.value,limit:6});if(project===state.projectId){results.value=data.items;warning.value=data.warning;usedMode.value=data.mode}}catch(e){if(project===state.projectId)warning.value=String(e)}finally{searching.value=false}}
watch(()=>state.projectId,()=>{info.value=null;settingsOpen.value=false;key.value='';reuse.value='';results.value=[];query.value='';warning.value='';usedMode.value='';void load()})
watch(()=>state.sources.map(s=>s.id+':'+s.archived).join('|'),()=>void load())
onMounted(()=>{void load();timer=setInterval(()=>{if(document.visibilityState==='visible'&&(running.value||Date.now()-lastLoaded>=15000))void load()},3000)})
onUnmounted(()=>{if(timer)clearInterval(timer);key.value=''})
</script>
<template>
 <div class="knowledge">
  <div class="knowledge-heading"><div><strong>资料语义检索 <span class="optional">可选</span></strong><p>手动建立当前项目全部可读资料的索引。新增资料不会自动向量化，对话附件不参与。</p></div><div class="knowledge-actions"><button v-if="info?.canConfigure" class="text-button" @click="configure">RAG 设置</button><button v-if="running" class="button secondary" :disabled="working" @click="cancel">取消索引</button><button class="button secondary" :disabled="working||running" @click="index">{{running?'正在建立索引…':info?.built?'更新 RAG 索引':'建立 RAG 索引'}}</button></div></div>
  <p v-if="error" class="attachment-warning">{{error}}</p>
  <div v-if="info" class="knowledge-status" role="status"><span>{{info.built?'已索引 '+info.indexed+' 份 / '+info.chunks+' 个片段':'尚未建立索引，继续使用关键词检索'}}</span><span v-if="info.pending">{{info.pending}} 份待更新</span><span v-if="info.unreadable">{{info.unreadable}} 份文件不可解析，保留原件</span><span>可读正文约 {{info.characters.toLocaleString()}} 字</span></div>
  <p v-if="info?.job" class="form-hint knowledge-progress">{{info.job.summary}}<template v-if="info.job.tokens"> · 已发布片段用量 {{info.job.tokens.toLocaleString()}} token</template></p>
  <p v-for="failure in info?.job?.failures||[]" :key="failure.id" class="attachment-warning">{{failure.name}}：{{failure.message}}</p>
  <form class="knowledge-search" @submit.prevent="search"><input v-model="query" maxlength="200" required placeholder="搜索需求，例如：输错几次密码会锁号？" aria-label="搜索资料正文"/><select v-model="mode" aria-label="资料检索方式"><option value="auto">自动检索</option><option value="keyword">关键词</option><option value="hybrid">混合检索</option><option value="semantic">语义检索</option></select><button class="button secondary" :disabled="searching">{{searching?'检索中…':'检索资料'}}</button></form>
  <p v-if="warning" class="attachment-warning">{{warning}}</p><p v-if="usedMode" class="form-hint">{{usedMode==='hybrid'?'关键词与语义混合检索':usedMode==='semantic'?'语义检索':usedMode==='keyword_fallback'?'语义检索暂不可用，使用词法检索':'关键词检索'}} · {{results.length}} 个结果</p>
  <article v-for="(hit,i) in results" :key="hit.chunkId||hit.id+':'+i" class="knowledge-hit"><button class="attachment-name" @click="emit('read',hit.id,hit.start||0)">{{hit.name}}</button><small v-if="hit.chunkId">{{hit.heading||'正文片段'}} · 第 {{hit.start+1}}～{{hit.end}} 字 · 内容版本 {{hit.version.slice(0,8)}}</small><p>{{hit.summary}}</p><button class="text-button" @click="emit('read',hit.id,hit.start||0)">查看原文上下文</button></article>
 </div>
 <AppModal :open="settingsOpen" title="项目资料 RAG 设置" @close="settingsOpen=false;key=''">
  <form @submit.prevent="save"><div class="modal-body form-grid"><p class="form-hint">配置与聊天模型分开，项目成员共用此资料索引。密钥存于{{localClient.mode==='server'?'服务器':'本机'}}执行服务，不写入项目数据库。保存配置不会调用模型；建库和测试连接会消耗向量额度。</p><label class="field-label">Base URL<input v-model="form.baseUrl" readonly/></label><label class="field-label">向量模型<input v-model="form.model" readonly/></label><p class="attachment-warning">固定使用 Plan 套餐地址，不自动切换按量计费地址。供应商控制台开启超额后付费时，额度耗尽仍可能收费。</p><label class="field-label">API Key<input v-model="key" type="password" autocomplete="off" maxlength="2048" :placeholder="info?.hasKey?'已保存；留空沿用':'填写 Plan 专属 Key'"/></label><label v-if="compatibleModels.length" class="field-label">或复用相同地址的已有密钥<select v-model="reuse"><option value="">不复用</option><option v-for="m in compatibleModels" :key="m.id" :value="m.id">{{m.name}}</option></select></label><label class="field-label">每段最多字符<input v-model.number="form.chunkSize" type="number" min="300" max="2400" required/></label><label class="field-label">相邻段落重叠字符<input v-model.number="form.overlap" type="number" min="0" max="500" required/></label><label class="field-label">每次索引正文上限（字）<input v-model.number="form.maxCharacters" type="number" min="1000" max="1000000" required/></label><p class="form-hint">修改分段配置后需手动更新索引。测试连接使用已保存的配置和密钥。</p></div><div class="modal-actions"><button v-if="info?.config" class="button secondary" type="button" :disabled="working" @click="test">测试已保存配置</button><button class="button primary" :disabled="working">{{working?'处理中…':'保存配置'}}</button></div></form>
 </AppModal>
</template>
<style scoped>
.knowledge{padding:20px 24px;border-bottom:1px solid var(--border);background:var(--panel,#fff)}.knowledge-heading{display:flex;justify-content:space-between;gap:20px;align-items:flex-start}.knowledge-heading p{font-size:12px;color:var(--muted);line-height:1.7;margin:8px 0}.optional{font-size:11px;font-weight:400;color:var(--muted);margin-left:8px}.knowledge-actions{display:flex;gap:14px;align-items:center;flex-shrink:0}.knowledge-status{display:flex;gap:16px;flex-wrap:wrap;color:var(--muted);font-size:12px;margin:10px 0}.knowledge-progress{margin:8px 0}.knowledge-search{display:flex;gap:10px;margin-top:16px}.knowledge-search input{flex:1;min-width:100px}.knowledge-search select{width:130px}.knowledge-hit{padding:14px 0;border-top:1px solid var(--border);margin-top:12px}.knowledge-hit small{display:block;color:var(--muted);font-size:11px;margin:6px 0}.knowledge-hit p{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px;line-height:1.7;margin:8px 0;color:var(--text)}@media(max-width:800px){.knowledge-heading{flex-wrap:wrap}.knowledge-search{flex-wrap:wrap}.knowledge-search input{flex-basis:100%}}
</style>
