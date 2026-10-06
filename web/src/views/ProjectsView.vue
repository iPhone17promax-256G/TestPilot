<script setup lang="ts">
import { ref,computed } from 'vue'
import AppIcon from '../components/AppIcon.vue'
import AppModal from '../components/AppModal.vue'
import ProjectSources from '../components/ProjectSources.vue'
import { state, ui, notify, saveEnvironment, request, switchProject } from '../store'
const tab=ref('environments')
const sourceCount=computed(()=>state.sources.filter(doc=>!doc.archived).length)
const open = ref(false)
const editing = ref('')
const form = ref({ name: '', url: '', executionUrl: '', description: '' })
function edit(id = '') { editing.value = id; const env = state.environments.find(e => e.id === id); form.value = env ? { name: env.name, url: env.url, executionUrl: env.executionUrl, description: env.description } : { name: '', url: '', executionUrl: '', description: '' }; open.value = true }
async function save() {
  if (!form.value.name.trim()) return notify('请填写环境名称')
  try {await saveEnvironment({name:form.value.name,url:form.value.url,execution_url:form.value.executionUrl,description:form.value.description,environment_id:editing.value});open.value=false}catch{}
}
const projectOpen=ref(false),projectId=ref(''),project=ref({name:'',description:''})
function editProject(id=state.projectId){const p=state.projects.find(p=>p.id===id);projectId.value=p?.id||'';project.value={name:p?.name||'',description:p?.description||''};projectOpen.value=true}
async function saveProject(){try{const p=await request('/projects',{...project.value,project_id:projectId.value});await switchProject(p.id);projectOpen.value=false;notify('项目已保存')}catch(error){notify(String(error))}}

const membersOpen=ref(false),members=ref<string[]>([]),memberChoices=ref<{id:string;username:string}[]>([])
const ownProject=computed(()=>state.projects.find(p=>p.id===state.projectId)?.ownerId===ui.user?.id)
async function editMembers(){try{const data=await request('/projects/'+state.projectId+'/members');members.value=data.members;memberChoices.value=data.accounts;membersOpen.value=true}catch(e){notify(String(e))}}
async function saveMembers(){try{await request('/projects/'+state.projectId+'/members',{members:members.value});membersOpen.value=false;notify('项目成员已保存')}catch(e){notify(String(e))}}
</script>
<template>
  <div class="page"><div class="page-heading"><div><h1>项目与环境</h1><p>切换被测项目，管理运行环境与 Agent 可读取的资料。</p></div><button class="button primary" @click="editProject('')">新建项目</button></div>
    <p v-if="ui.deploymentMode==='server'" class="form-hint">新项目默认仅创建者可见，可通过“项目成员”共享；MM 演示项目及升级前未设置归属的项目为公共项目。</p><div class="project-card-list"><button v-for="p in state.projects" :key="p.id" class="project-card" :class="{active:p.id===state.projectId}" :aria-pressed="p.id===state.projectId" @click="switchProject(p.id)"><strong>{{p.name}}</strong><p>{{p.description||'尚未填写项目说明'}}</p><small>{{p.id===state.projectId?'当前项目':'点击切换'}}</small></button></div>
    <div class="project-context-heading"><div><h2>{{state.projectName}}</h2><p class="form-hint">{{state.projectDescription}}</p></div><button v-if="ui.deploymentMode==='server'&&ownProject" class="button secondary" @click="editMembers">项目成员</button><button class="button secondary" @click="editProject()">编辑项目</button></div>
    <div class="local-tabs project-tabs" role="tablist" aria-label="项目资料分类"><button :class="{active:tab==='environments'}" role="tab" :aria-selected="tab==='environments'" @click="tab='environments'">运行环境 <span>{{state.environments.length}}</span></button><button :class="{active:tab==='sources'}" role="tab" :aria-selected="tab==='sources'" @click="tab='sources'">项目资料 <span>{{sourceCount}}</span></button></div>
    <div class="project-columns"><section v-if="tab==='environments'" class="panel"><div class="panel-heading"><h2><AppIcon name="projects" :size="17" />运行环境</h2><button class="text-button" @click="edit()"><AppIcon name="plus" :size="15" />添加环境</button></div><div v-for="env in state.environments" :key="env.id" class="environment-row"><span class="environment-icon"><AppIcon name="web" :size="19" /></span><div><h3>{{ env.name }}</h3><code>{{ env.url }}</code><p>{{ env.description }}</p></div><button class="text-button" @click="edit(env.id)">编辑</button></div><div v-if="!state.environments.length" class="empty-state"><h3>添加一个测试环境</h3><p>填写被测系统的地址。</p></div><div class="section-footnote">环境是同一被测项目的不同部署，例如本地、测试或预发布。</div></section><ProjectSources v-else/></div>
    <details class="advanced-section"><summary>测试账号与变量引用</summary><p class="form-hint">测试账号通过服务端环境变量配置。步骤中可使用 <code v-pre>{{env.TEST_USERNAME}}</code> 和 <code v-pre>{{env.TEST_PASSWORD}}</code> 引用。</p></details>
    <AppModal :open="open" :title="editing ? '编辑运行环境' : '添加运行环境'" @close="open = false"><form @submit.prevent="save"><div class="modal-body form-grid"><label class="field-label">环境名称<input v-model="form.name" required maxlength="80" placeholder="例如：MM 测试环境" /></label><label class="field-label">浏览器访问地址<input v-model="form.url" required type="url" placeholder="http://127.0.0.1:8065" /></label><label class="field-label">测试器访问地址<input v-model="form.executionUrl" type="url" placeholder="http://mm-target:8065" /><span class="form-hint">测试容器访问被测系统的地址。例如浏览器使用 127.0.0.1:8065，测试器使用 mm-target:8065；留空使用浏览器地址。</span></label><label class="field-label">说明<textarea v-model="form.description" rows="3" maxlength="500" placeholder="这个环境的用途和注意事项" /></label></div><div class="modal-actions"><button type="button" class="button secondary" @click="open = false">取消</button><button type="submit" class="button primary">保存配置</button></div></form></AppModal>
    <AppModal :open="projectOpen" :title="projectId?'编辑项目':'新建项目'" @close="projectOpen=false"><form @submit.prevent="saveProject"><div class="modal-body form-grid"><label class="field-label">项目名称<input v-model="project.name" required maxlength="120" /></label><label class="field-label">项目说明<textarea v-model="project.description" maxlength="2000" rows="3" /></label></div><div class="modal-actions"><button class="button primary" type="submit">保存项目</button></div></form></AppModal>
    <AppModal :open="membersOpen" title="项目成员" @close="membersOpen=false"><form @submit.prevent="saveMembers"><div class="modal-body form-grid"><p class="form-hint">成员共同维护项目用例与资料，各人的对话、运行记录和模型配置分别保存。</p><label v-for="account in memberChoices" :key="account.id" class="step-enable"><input v-model="members" type="checkbox" :value="account.id"/>{{account.username}}</label><p v-if="!memberChoices.length" class="muted">暂无其他账号，请管理员先创建。</p></div><div class="modal-actions"><button class="button primary">保存成员</button></div></form></AppModal>
  </div>
</template>
