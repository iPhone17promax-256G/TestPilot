<script setup lang="ts">
import {computed,ref,watch} from 'vue'
import AppIcon from './AppIcon.vue'
import AppModal from './AppModal.vue'
import ProjectKnowledge from './ProjectKnowledge.vue'
import type {ProjectSource} from '../types'
import {state,request,refresh,notify,uploadProjectSource} from '../store'
import {dateTime} from '../uiHelpers'
const query=ref(''),archived=ref(false),input=ref<HTMLInputElement>(),uploading=ref(false),progress=ref('')
const sourceOpen=ref(false),source=ref({name:'',content:''}),saving=ref(false),busy=ref('')
const preview=ref<ProjectSource|null>(null),content=ref(''),nextOffset=ref<number|null>(null),loading=ref(false),error=ref('')
const documents=computed(()=>state.sources.filter(doc=>doc.archived===archived.value&&(doc.name+' '+doc.preview).toLocaleLowerCase().includes(query.value.toLocaleLowerCase())))
const activeCount=computed(()=>state.sources.filter(doc=>!doc.archived).length)
const usedBytes=computed(()=>state.sources.reduce((sum,doc)=>sum+doc.size,0))
function size(bytes:number){return bytes>=1024*1024?(bytes/1024/1024).toFixed(1)+' MB':(bytes/1024).toFixed(1)+' KB'}
watch(()=>state.projectId,()=>{query.value='';preview.value=null;sourceOpen.value=false;source.value={name:'',content:''};archived.value=false;progress.value=''})
async function upload(event:Event){
 const files=Array.from((event.target as HTMLInputElement).files||[]);(event.target as HTMLInputElement).value=''
 if(!files.length)return
 if(files.length>5)return notify('每次最多选择 5 个文件')
 const project=state.projectId;uploading.value=true;let saved=0;const failures:string[]=[]
 try{
  for(const file of files){
   // A project switch must not silently send the remaining files to another project.
   if(project!==state.projectId)break
   progress.value='正在上传 '+file.name
   try{await uploadProjectSource(file,project);saved++}catch(e){failures.push(file.name+'：'+String(e).replace(/^Error: /,''))}
  }
  if(project===state.projectId){await refresh(true);progress.value=failures.length?failures.join('；'):`已保存 ${saved} 个文件`;notify(progress.value)}
 }finally{uploading.value=false}
}
async function saveSource(){
 const project=state.projectId;saving.value=true
 try{await request('/sources',source.value);if(project===state.projectId){await refresh(true);sourceOpen.value=false;source.value={name:'',content:''};notify('项目文字资料已保存')}}catch(e){notify(String(e))}finally{saving.value=false}
}
async function archive(doc:ProjectSource){
 const project=state.projectId;busy.value=doc.id
 try{await request('/sources/'+doc.id+'/archive',{archived:!doc.archived});if(project===state.projectId){await refresh(true);notify(doc.archived?'资料已恢复':'资料已归档，可在已归档中恢复')}}catch(e){notify(String(e))}finally{busy.value=''}
}
async function load(offset=0){
 const doc=preview.value;if(!doc)return
 const project=state.projectId;loading.value=true;error.value=''
 try{
  const data=await request<{content:string;nextOffset:number|null}>('/sources/'+doc.id+'/text?offset='+offset)
  if(project===state.projectId&&preview.value?.id===doc.id){content.value=offset?content.value+data.content:data.content;nextOffset.value=data.nextOffset}
 }catch(e){if(project===state.projectId&&preview.value?.id===doc.id)error.value=String(e)}finally{if(preview.value?.id===doc.id)loading.value=false}
}
function view(doc:ProjectSource,offset=0){preview.value=doc;content.value='';nextOffset.value=null;error.value='';loading.value=false;if(doc.readable&&!doc.archived)void load(offset)}
function readHit(id:string,offset:number){const doc=state.sources.find(s=>s.id===id);if(doc)view(doc,offset)}
</script>
<template>
 <section class="panel project-materials">
  <div class="panel-heading"><h2><AppIcon name="file" :size="17"/>项目资料</h2><div class="material-actions"><button class="button secondary" @click="sourceOpen=true">新建文字资料</button><button class="button primary" :disabled="uploading" @click="input?.click()"><AppIcon name="plus" :size="15"/>{{uploading?'上传中…':'上传文件'}}</button><input ref="input" type="file" multiple hidden aria-label="上传项目文件" @change="upload"/></div></div>
  <p class="section-footnote">需求、接口说明与配套文件保存在当前项目，所有对话均可按需读取。对话附件仍只用于它所属的对话。</p>
  <ProjectKnowledge @read="readHit"/>
  <div class="material-toolbar"><input v-model="query" aria-label="搜索项目资料" placeholder="搜索名称和简介"/><div class="local-tabs" role="tablist" aria-label="资料状态"><button role="tab" :aria-selected="!archived" :class="{active:!archived}" @click="archived=false">使用中 <span>{{activeCount}}</span></button><button role="tab" :aria-selected="archived" :class="{active:archived}" @click="archived=true">已归档 <span>{{state.sources.length-activeCount}}</span></button></div></div>
  <p v-if="progress" class="material-progress" role="status">{{progress}}</p>
  <div v-for="doc in documents" :key="doc.id" class="material-row">
   <span class="material-file-icon"><AppIcon name="file" :size="20"/></span>
   <div class="material-info"><button class="attachment-name" @click="view(doc)">{{doc.name}}</button><small>{{doc.storage==='text'?'文字资料':doc.type.toUpperCase()}} · {{size(doc.size)}}<span v-if="doc.readable"> · {{doc.characters.toLocaleString()}} 字</span><span v-if="doc.createdAt"> · {{dateTime(doc.createdAt)}}</span></small><p v-if="doc.preview">{{doc.preview.slice(0,100)}}</p><span v-if="!doc.readable" class="attachment-warning">仅保存原文件 · 暂不可读取正文</span><span v-else-if="doc.warnings.length" class="attachment-warning">解析有提醒，请预览核对</span></div>
   <div class="material-actions"><button class="text-button" @click="view(doc)">{{doc.readable&&!doc.archived?'预览':'详情'}}</button><a class="text-button" :href="doc.downloadUrl" download>下载</a><button class="text-button" :disabled="!!busy" @click="archive(doc)">{{busy===doc.id?'处理中…':doc.archived?'恢复':'归档'}}</button></div>
  </div>
  <div v-if="!documents.length" class="empty-state"><h3>{{query?'没有匹配的资料':archived?'暂无归档资料':'保存项目的测试依据'}}</h3><p>{{archived?'归档后不再提供给 Agent，原文件可下载、恢复。':'上传需求文档、接口说明或配套文件，也可以直接编写文字资料。'}}</p></div>
  <div class="section-footnote material-limits">单个文件 ≤ 10 MB，每项目 ≤ 200 份 / 200 MB（含归档）；已用 {{size(usedBytes)}}。PDF、DOCX、TXT、Markdown、CSV、JSON、YAML 可解析正文，扫描件暂无 OCR，其他格式保存原件供下载。</div>
 </section>
 <AppModal :open="sourceOpen" title="新建项目文字资料" @close="sourceOpen=false"><form @submit.prevent="saveSource"><div class="modal-body form-grid"><label class="field-label">资料名称<input v-model="source.name" required maxlength="120"/></label><label class="field-label">正文<textarea v-model="source.content" required maxlength="50000" rows="10"/></label></div><div class="modal-actions"><button class="button secondary" type="button" @click="sourceOpen=false">取消</button><button class="button primary" type="submit" :disabled="saving">{{saving?'保存中…':'保存资料'}}</button></div></form></AppModal>
 <AppModal :open="!!preview" :title="preview?.name||'资料预览'" @close="preview=null"><template v-if="preview"><div class="modal-body attachment-preview"><p class="form-hint">{{preview.storage==='text'?'文字资料':preview.type.toUpperCase()+' 文件'}} · {{size(preview.size)}}<span v-if="preview.readable"> · 已解析 {{preview.characters.toLocaleString()}} 字{{preview.pages?' / '+preview.pages+' 页':''}}</span></p><p v-if="preview.archived" class="attachment-warning">资料已归档，不再提供给 Agent；恢复后可预览正文。</p><p v-for="warning in preview.warnings" :key="warning" class="attachment-warning">{{warning}}</p><p v-if="!preview.readable" class="form-hint">原文件已保存，可以下载核对。需要 Agent 使用时，请再上传可解析的文字版。</p><pre v-if="content">{{content}}</pre><p v-if="error" class="attachment-warning">{{error}}</p><p v-if="loading" role="status" class="form-hint">正在读取正文…</p><button v-if="nextOffset!==null" type="button" class="button secondary" :disabled="loading" @click="load(nextOffset!)">继续读取正文</button></div><div class="modal-actions"><a :href="preview.downloadUrl" class="button secondary" download>下载原文件</a><button class="button primary" type="button" @click="preview=null">关闭预览</button></div></template></AppModal>
</template>
<style scoped>
.project-materials>.section-footnote:first-of-type{border:0;padding-top:0}
.material-toolbar{display:flex;align-items:center;justify-content:space-between;gap:24px;padding:12px 24px;border-bottom:1px solid var(--border)}
.material-toolbar>input{max-width:360px}.material-toolbar .local-tabs{margin:0;flex-shrink:0}
.material-row{display:flex;align-items:center;gap:16px;padding:20px 24px;border-bottom:1px solid var(--border)}
.material-file-icon{align-self:flex-start;padding:10px;background:#f5f6f8;border-radius:8px;color:var(--muted)}
.material-info{flex:1;min-width:0}.material-info>.attachment-name{display:block;text-align:left;overflow-wrap:anywhere;font-weight:600}
.material-info small{display:block;color:var(--muted);font-size:12px;margin:6px 0}.material-info p{color:var(--muted);font-size:13px;margin:6px 0 0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.material-actions{display:flex;align-items:center;gap:14px;flex-shrink:0}.material-progress{padding:0 24px;overflow-wrap:anywhere;font-size:13px;color:var(--muted)}
.material-limits{line-height:1.8}.material-row:last-of-type{border-bottom:0}
@media(max-width:800px){.material-toolbar{flex-wrap:wrap;gap:8px}.material-toolbar>input{max-width:none;width:100%}.material-row{flex-wrap:wrap}.material-row>.material-actions{margin-left:54px}.panel-heading{align-items:flex-start;gap:12px;flex-wrap:wrap}.material-actions{gap:12px}}
</style>
