<script setup lang="ts">
import {ref,watch} from 'vue'
import {useRoute,useRouter} from 'vue-router'
import AppIcon from './components/AppIcon.vue'
import {localClient,runtimeLabel} from './localClient'
import AppModal from './components/AppModal.vue'
import LoginScreen from './components/LoginScreen.vue'
import {state,ui,refresh,notify,switchProject,logout} from './store'
const route=useRoute(),router=useRouter(),mobileNav=ref(false),about=ref(false)
const collapsed=ref(localStorage.getItem('testpilot-nav-collapsed')==='true')
watch(collapsed,value=>localStorage.setItem('testpilot-nav-collapsed',String(value)))
const accountOpen=ref(false),projectSwitching=ref(false)
async function changeProject(e:Event){const target=e.target as HTMLSelectElement,id=target.value;if(projectSwitching.value||id===state.projectId)return;projectSwitching.value=true;try{if(route.path!=='/agent'){const failure=await router.push('/agent');if(failure)return}await switchProject(id)}catch(error){notify(String(error))}finally{target.value=state.projectId;projectSwitching.value=false}}
async function signOut(){try{await logout();router.push('/agent')}catch(e){notify(String(e))}}
const navigation=[{path:'/agent',label:'Agent 工作台',icon:'sparkles'},{path:'/cases',label:'测试用例',icon:'cases'},{path:'/runs',label:'运行记录',icon:'play'},{path:'/mobile',label:'应用与设备',icon:'app'},{path:'/projects',label:'项目与环境',icon:'projects'},{path:'/environment',label:'运行环境',icon:'shield'},{path:'/settings',label:'设置',icon:'settings'}]
watch(()=>route.path,()=>{mobileNav.value=false;accountOpen.value=false})
</script>
<template>
<LoginScreen v-if="ui.authRequired&&!ui.loading"/>
<div v-else class="app-shell" :class="{'nav-collapsed':collapsed}">
<a class="skip-link" href="#main-content">跳至主要内容</a>
<button v-if="mobileNav" class="nav-backdrop" aria-label="关闭导航" @click="mobileNav=false"/>
<aside id="main-navigation" class="sidebar" :class="{'is-open':mobileNav}" aria-label="主导航">
<RouterLink to="/agent" class="brand" aria-label="TestPilot 首页"><span class="brand-mark"><AppIcon name="cases" :size="22"/></span><span class="brand-word">TestPilot</span></RouterLink>
<nav><template v-for="(item,index) in navigation" :key="item.path"><div v-if="index===0||index===4" class="nav-group-label">{{index===0?'测试工作':'工作空间'}}</div><RouterLink :to="item.path" class="nav-item" :title="item.label" :aria-label="item.label" :class="{active:route.path.startsWith(item.path)}" :aria-current="route.path.startsWith(item.path)?'page':undefined"><AppIcon :name="item.icon"/><span class="nav-name">{{item.label}}</span></RouterLink></template></nav>
<div class="sidebar-bottom"><RouterLink to="/environment" class="sidebar-connection" :class="{'is-connected':localClient.connected}" :title="localClient.connected?runtimeLabel+'已连接':runtimeLabel+'未连接'"><i/><span>{{localClient.connected?runtimeLabel+'已连接':'执行器未连接'}}</span></RouterLink><button class="nav-item nav-collapse" :aria-label="collapsed?'展开侧栏':'收起侧栏'" @click="collapsed=!collapsed"><AppIcon name="collapse"/><span class="nav-name">收起侧栏</span></button><button class="nav-item" aria-label="查看平台说明" @click="about=true"><AppIcon name="info"/><span class="nav-name">关于 TestPilot</span></button></div>
</aside>
<div class="main-shell"><header class="topbar"><div class="breadcrumb"><button class="icon-button mobile-menu" aria-label="打开导航" :aria-expanded="mobileNav" aria-controls="main-navigation" @click="mobileNav=!mobileNav"><AppIcon name="menu"/></button><select class="project-select topbar-project" aria-label="切换项目" :disabled="ui.loading||projectSwitching" :value="state.projectId" @change="changeProject"><option v-for="p in state.projects" :key="p.id" :value="p.id">{{p.name}}</option></select><AppIcon name="chevron" :size="14"/><strong>{{route.meta.title}}</strong></div><div class="topbar-actions"><button class="icon-button" aria-label="刷新工作空间" :disabled="ui.loading" @click="refresh(true)"><AppIcon name="reset" :size="17"/></button><span v-if="ui.deploymentMode==='local'" class="plain-tag">本地工作空间</span><div v-else class="account-control" @keydown.esc="accountOpen=false"><button class="account-button" aria-label="账号菜单" :aria-expanded="accountOpen" @click="accountOpen=!accountOpen"><span class="profile-avatar">{{ui.user?.username.slice(0,1).toUpperCase()}}</span><span>{{ui.user?.username}}</span><AppIcon name="down" :size="14"/></button><div v-if="accountOpen" class="account-menu"><RouterLink to="/settings?tab=account" @click="accountOpen=false">账号设置</RouterLink><button @click="signOut">退出登录</button></div><button v-if="accountOpen" class="menu-dismiss" aria-label="关闭账号菜单" @click="accountOpen=false"/></div></div></header>
<div v-if="!ui.connected&&!ui.loading&&!ui.authRequired" class="connection-alert" role="alert"><AppIcon name="info" :size="16"/><span>暂时无法连接服务器，正在保留当前页面。</span><button class="text-button" @click="refresh(true)">重试</button></div>
<main id="main-content"><div v-if="!ui.initialized" class="page"><div class="panel empty-state"><AppIcon :name="ui.loading?'running':'info'" :size="28"/><h3>{{ui.loading?'正在加载工作空间':'暂时无法读取数据'}}</h3><p>{{ui.loading?'正在读取项目、用例和报告。':'请检查服务连接后重试。'}}</p><button v-if="!ui.loading" class="button secondary" @click="refresh(true)">重试</button></div></div><RouterView v-else/></main></div>
<div v-if="ui.toast" class="toast" role="status"><AppIcon name="info" :size="17"/>{{ui.toast}}</div>
<AppModal :open="about" title="关于 TestPilot" @close="about=false"><div class="modal-body"><p>TestPilot · v0.2</p><p class="form-hint">API、Web UI、JMeter 性能与 Android APP 测试，支持持续对话的测试 Agent。</p><ul class="plain-list"><li>可视化步骤与原生脚本共用用例版本和执行报告。</li><li>测试在独立容器运行，日志、截图与指标作为证据保留。</li><li>项目、对话和报告保存在 MySQL。</li></ul></div><div class="modal-actions"><button class="button primary" @click="about=false">关闭</button></div></AppModal>
</div></template>
