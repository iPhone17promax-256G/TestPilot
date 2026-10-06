<script setup lang="ts">
import {ref,onMounted} from 'vue'
import AppIcon from './AppIcon.vue'
import AppModal from './AppModal.vue'
import {localClient,clientRequest,refreshClient,chooseModel,type ModelProfile} from '../localClient'
import {notify,refresh} from '../store'
const choices=ref<any[]>([]),busy=ref(false),error=ref(''),apiKey=ref(''),testing=ref(''),result=ref(''),resultId=ref(''),testFailed=ref(false)
const editorOpen=ref(false),loading=ref(false),catalogError=ref('')
const fresh=()=>({id:'',name:'',provider:'openrouter',baseUrl:'https://openrouter.ai/api/v1',model:'',maxSteps:24,maxTokens:12000,default:false,freeOnly:true,vision:true,tools:true,reasoningOptions:[],reasoningSource:'unknown',extraBody:{}} as ModelProfile)
const form=ref(fresh())
async function loadCatalog(){if(loading.value||(!localClient.managed&&!localClient.token))return;loading.value=true;catalogError.value='';try{choices.value=await clientRequest('/catalog')}catch{catalogError.value='暂时无法获取模型目录，请稍后重试。'}finally{loading.value=false}}
onMounted(()=>refreshClient(true))
function create(){form.value=fresh();apiKey.value='';error.value='';editorOpen.value=true;void loadCatalog()}
function edit(profile:ModelProfile){const {hasKey,ownerId,...rest}=profile as any;form.value={...rest};apiKey.value='';error.value='';editorOpen.value=true;if(profile.provider==='openrouter')void loadCatalog()}
function close(){if(busy.value)return;editorOpen.value=false;apiKey.value='';error.value=''}
function providerChanged(){form.value.baseUrl=form.value.provider==='openrouter'?'https://openrouter.ai/api/v1':'';form.value.model='';form.value.freeOnly=form.value.provider==='openrouter';form.value.reasoningOptions=[];if(form.value.provider==='openrouter')void loadCatalog()}
function selectModel(){const item=choices.value.find(m=>m.id===form.value.model);if(item){form.value.vision=item.vision;form.value.tools=item.tools;if(!form.value.name)form.value.name=item.name}}
async function save(){busy.value=true;error.value='';try{const saved=await clientRequest('/models',{profile:form.value,apiKey:apiKey.value});apiKey.value='';await refreshClient(true);await refresh(true);localClient.modelId=saved.id;chooseModel();form.value=fresh();editorOpen.value=false;notify('配置已保存，密钥留在实际执行端')}catch(e){error.value=String(e)}finally{busy.value=false}}
async function test(profile:ModelProfile){testing.value=profile.id;resultId.value=profile.id;result.value='';testFailed.value=false;try{const r=await clientRequest('/models/'+profile.id+'/test',{});result.value=r.message+' · '+r.model+' · '+r.duration+'s'+(r.visionSent?' · 已发送图片':'')}catch(e){testFailed.value=true;result.value=String(e)}finally{testing.value=''}}
async function remove(profile:ModelProfile){if(!confirm('删除这个模型配置及保存的密钥？'))return;try{await clientRequest('/models/'+profile.id+'/delete',{});await refreshClient(true);await refresh(true);notify('模型配置已删除')}catch(e){notify(String(e))}}
</script>

<template>
  <section id="settings-panel-model" role="tabpanel" aria-labelledby="settings-model" class="panel model-manager">
    <div class="model-manager-heading"><div><h2>我的模型 <span class="heading-count">{{localClient.models.length}}</span></h2><p>{{localClient.mode==='local'?'本地工作空间的模型，可在对话中按需切换。':'配置只属于当前账号，可在对话中按需切换。'}}</p></div><button class="button primary" :disabled="!localClient.connected" @click="create"><AppIcon name="plus" :size="16"/>添加模型</button></div>
    <div v-if="!localClient.connected" class="model-connection-notice"><AppIcon name="info" :size="17"/><span>先在<RouterLink to="/environment">运行环境</RouterLink>连接执行器，再管理模型配置。</span></div>
    <div class="model-profile-list">
      <article v-for="profile in localClient.models" :key="profile.id" class="model-profile-card">
        <div class="model-profile-main"><span class="resource-icon"><AppIcon :name="profile.provider==='openrouter'?'chain':'sparkles'" :size="21"/></span><div class="model-profile-info"><div class="resource-title"><h3>{{profile.name}}</h3><span v-if="profile.default" class="plain-tag">默认模型</span></div><code>{{profile.model}}</code><div class="model-capabilities"><span>{{profile.provider==='openrouter'?'OpenRouter':'兼容服务'}}</span><span v-if="profile.vision"><AppIcon name="image" :size="13"/>图片输入</span><span v-if="profile.tools"><AppIcon name="terminal" :size="13"/>工具调用</span><span>{{profile.reasoningOptions.length?'支持 '+profile.reasoningOptions.length+' 档思考深度':'默认思考深度'}}</span></div></div><span class="health-badge" :class="profile.hasKey?'ready':'missing'"><i/>{{profile.hasKey?'已配置密钥':'待配置密钥'}}</span></div>
        <div class="model-profile-footer"><span class="muted">{{profile.hasKey?'在聊天输入区选择后使用':'填写自己的 API Key 后才能使用'}}</span><div class="row-actions"><button class="button secondary compact" :disabled="!localClient.connected" @click="edit(profile)">{{profile.hasKey?'编辑配置':'配置密钥'}}</button><button class="text-button" :disabled="!!testing||!profile.hasKey||!localClient.connected" @click="test(profile)"><AppIcon :name="testing===profile.id?'running':'play'" :size="14"/>{{testing===profile.id?'测试中…':'测试连接'}}</button><button class="icon-button danger" :aria-label="'删除模型 '+profile.name" :disabled="!!testing||!localClient.connected" @click="remove(profile)"><AppIcon name="trash" :size="16"/></button></div></div>
        <p v-if="resultId===profile.id&&result" class="model-test-result" :class="{failed:testFailed}" role="status"><AppIcon :name="testFailed?'info':'passed'" :size="16"/>{{result}}</p>
      </article>
      <div v-if="!localClient.models.length" class="empty-state"><AppIcon name="sparkles" :size="28"/><h3>添加第一个模型</h3><p>可选择 OpenRouter 免费视觉模型，或接入自己的兼容服务。</p><button class="button secondary" :disabled="!localClient.connected" @click="create">添加模型</button></div>
    </div>
    <div class="model-footnotes"><p><AppIcon name="shield" :size="15"/><span>密钥保存在实际执行端：{{localClient.storageMode||'连接后可查看保存方式'}}。{{localClient.mode==='server'?'服务端加密保存，按账号隔离。':'本机保存，不同步其他电脑。'}}</span></p><p><AppIcon name="info" :size="15"/><span>OpenRouter 免费模型也需要 API Key。连接测试会发送少量请求，付费模型可能消耗余额。</span></p></div>
  </section>

  <AppModal :open="editorOpen" :title="form.id?'编辑模型':'添加模型'" class="model-editor-modal" @close="close">
    <form autocomplete="off" @submit.prevent="save">
      <div class="modal-body form-grid">
        <div class="config-grid"><label class="field-label">名称<input v-model="form.name" name="model-display-name" autocomplete="off" required maxlength="120" placeholder="给这个配置起个名字"/></label><label class="field-label">供应商<select v-model="form.provider" class="select" @change="providerChanged"><option value="openrouter">OpenRouter</option><option value="compatible">自定义 OpenAI 兼容服务</option></select></label></div>
        <div v-if="form.provider==='openrouter'" class="model-catalog-field"><label class="field-label">免费模型<select v-model="form.model" class="select" required :disabled="loading" @change="selectModel"><option value="">{{loading?'正在加载模型目录…':'选择免费视觉模型'}}</option><option v-if="form.model&&!choices.some(choice=>choice.id===form.model)" :value="form.model">{{form.model}}（当前配置）</option><option v-for="choice in choices" :key="choice.id" :value="choice.id">{{choice.name}} · {{choice.id}}</option></select></label><div class="catalog-description"><p class="form-hint">只显示支持图片输入和工具调用的免费模型，不回退到付费模型。</p><button class="text-button" type="button" :disabled="loading" @click="loadCatalog"><AppIcon :name="loading?'running':'reset'" :size="14"/>刷新目录</button></div><p v-if="catalogError" class="inline-error" role="alert">{{catalogError}}</p><p v-else-if="!loading&&!choices.length" class="form-hint">当前没有可用的免费候选，请刷新目录或接入自定义服务。</p></div>
        <template v-else><label class="field-label">API 地址<input v-model="form.baseUrl" type="url" required placeholder="https://provider.example/v1"/></label><label class="field-label">模型 ID<input v-model="form.model" required placeholder="ark-code-latest"/></label><label class="step-enable"><input v-model="form.vision" type="checkbox"/>支持图片输入（保存后可测试连接验证）</label><p class="form-hint">兼容接口无法统一获取思考档位，目前提供“默认”。</p></template>
        <label class="field-label">API Key<input v-model="apiKey" type="password" name="provider-api-key" autocomplete="new-password" :placeholder="form.id?'留空保留已有密钥':'输入供应商的 API Key'" maxlength="2048"/><small class="form-hint">{{localClient.mode==='server'?'密钥经服务端转交执行服务加密保存，不返回页面或写入日志。生产部署请使用 HTTPS。':'只保存在本机执行端，不写入浏览器存储。删除本机执行数据会丢失密钥。'}}</small></label>
        <details class="model-advanced"><summary>运行预算 <span>{{form.maxSteps}} 次决策 · {{form.maxTokens}} 输出预算</span></summary><div class="config-grid"><label class="field-label">每轮决策上限<input v-model.number="form.maxSteps" type="number" min="5" max="50"/></label><label class="field-label">模型输出预算<input v-model.number="form.maxTokens" type="number" min="1000" max="32000"/></label></div></details>
        <label class="step-enable"><input v-model="form.default" type="checkbox"/>设为当前账号默认模型</label><p v-if="error" class="inline-error" role="alert">{{error}}</p>
      </div>
      <div class="modal-actions"><button type="button" class="button secondary" :disabled="busy" @click="close">取消</button><button class="button primary" :disabled="busy||!localClient.connected">{{busy?'保存中…':'保存模型'}}</button></div>
    </form>
  </AppModal>
</template>
