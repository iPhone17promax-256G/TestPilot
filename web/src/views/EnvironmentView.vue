<script setup lang="ts">
import {computed,ref,onMounted,onUnmounted} from 'vue'
import AppIcon from '../components/AppIcon.vue'
import {localClient,clientRequest,pairClient,refreshClient,runtimeLabel} from '../localClient'
import {notify} from '../store'
const busy=ref(false),error=ref(''),filter=ref('all'),code=ref(''),address=ref(localClient.address)
const job=ref<any>(null),installing=ref('')
const checks=computed(()=>localClient.report?.checks||[])
const readyCount=computed(()=>checks.value.filter((item:any)=>item.status==='ready').length)
const visibleChecks=computed(()=>checks.value.filter((item:any)=>filter.value==='all'||item.status!=='ready'))
const statusLabels:Record<string,string>={ready:'就绪',missing:'待安装',optional:'需设备检查',error:'异常'}
const canInstall=computed(()=>localClient.managed&&localClient.isAdmin&&localClient.connected)
async function check(smoke=false){busy.value=true;error.value='';try{localClient.report=await clientRequest('/environment',{smoke});await refreshClient();notify('环境检测完成')}catch(e){error.value=String(e)}finally{busy.value=false}}
async function pair(){busy.value=true;try{localClient.address=address.value;await pairClient(code.value);code.value=''}catch(e){error.value=String(e)}finally{busy.value=false}}
async function install(component:string,operation='install'){installing.value=component;error.value='';try{job.value=await clientRequest('/components/install',{component,operation})}catch(e){error.value=String(e);installing.value=''}}
async function poll(){if(!job.value||job.value.status!=='running')return;try{job.value=await clientRequest('/components/'+job.value.id);if(job.value.status!=='running'){installing.value='';await check()}}catch(e){error.value=String(e);installing.value=''}}
async function copy(value:string){try{await navigator.clipboard.writeText(value);notify('已复制')}catch{notify('请手动复制')}}
let timer:ReturnType<typeof setInterval>|undefined
onMounted(()=>{void refreshClient();timer=setInterval(()=>void poll(),2500)})
onUnmounted(()=>clearInterval(timer))
</script>
<template>
<div class="page environment-page">
 <div class="page-heading"><div><h1>运行环境</h1><p>{{localClient.mode==='local'?'检测这台电脑上的执行服务，无需拉取源码或手动配对。':'检测服务器上的执行服务，访问者电脑无需安装 Python、浏览器或测试工具。'}}</p></div><button class="button secondary" :disabled="busy||!localClient.connected" @click="check()"><AppIcon name="reset" :size="16"/>{{busy?'检测中…':'重新检测'}}</button></div>
 <section class="panel executor-panel">
  <div class="executor-overview"><span class="resource-icon"><AppIcon name="web" :size="22"/></span><div class="executor-description"><div class="resource-title"><h2>{{runtimeLabel}}</h2><span class="health-badge" :class="localClient.connected?'ready':'missing'"><i/>{{localClient.connected?'已连接':'未就绪'}}</span></div><p>{{localClient.mode==='local'?'Agent、浏览器操作与测试都在本机执行。':'Agent、浏览器操作与测试由服务器执行，账号任务分别调度。'}}</p><code v-if="localClient.managed">通过平台自动连接 · 无需配对</code></div><span class="plain-tag">{{localClient.mode==='local'?'本地模式 · 免登录':'服务端模式 · 账号登录'}}</span></div>
  <form v-if="!localClient.managed" class="executor-pair-form" @submit.prevent="pair"><label class="field-label">旧版执行器地址<input v-model="address" type="url" required/></label><label class="field-label">配对码<input v-model="code" type="password" required/></label><button class="button secondary" :disabled="busy">配对旧版执行器</button></form>
  <p v-if="error||localClient.error" class="inline-error executor-notice" role="alert">{{error||localClient.error}}</p>
  <div class="executor-footnote"><AppIcon name="shield" :size="15"/><span>密钥保存方式：{{localClient.storageMode||'执行服务连接后显示'}}。</span></div>
 </section>
 <section class="panel settings-card"><h2>安装与维护</h2><p class="form-hint">基础发布包包含 Python、HTTPX、Playwright、browser-use 与 Chromium。JMeter 和 Android 模拟器按需安装。Docker、虚拟化支持与真机驱动需要宿主机提供。</p><p v-if="localClient.mode==='server'&&!localClient.isAdmin" class="form-hint">当前账号可查看检测结果；安装和启动组件需由服务器管理员操作。</p><div class="row-actions"><button class="button secondary" :disabled="!canInstall||!!installing" @click="install('performance')">安装 JMeter</button><button class="button secondary" :disabled="!canInstall||!!installing" @click="install('app')">安装 Android 组件</button><button class="button secondary" :disabled="!canInstall||!!installing" @click="install('app','start')">启动内置模拟器</button></div><p class="form-hint">在线安装使用部署配置中固定的镜像地址；离线使用发布包的 import-images.sh 导入镜像。模拟器需要 /dev/kvm；可接入外部 Appium。</p><details class="capability-guide"><summary>基础环境修复与服务日志</summary><div><pre>bash install.sh --repair
docker compose --env-file install.env logs --tail=100 api executor</pre><button class="text-button" @click="copy('bash install.sh --repair')">复制修复命令</button><p class="form-hint">在安装目录执行。修复重新创建 API/执行服务并保留数据；不改系统驱动。浏览器不需要获取代码文件。</p></div></details><div v-if="job" role="status"><h3>组件操作 · {{job.status==='running'?'进行中':job.status==='completed'?'完成':'失败'}}</h3><pre>{{job.logs.join('\n')}}</pre></div></section>
 <section v-if="checks.length" class="capabilities-section">
  <div class="section-heading capability-heading"><div><h2>运行能力 <span class="heading-count">{{readyCount}} / {{checks.length}} 就绪</span></h2><p class="form-hint">最近检测：{{new Date(localClient.report.checkedAt).toLocaleString()}} · {{localClient.report.os}}</p></div><button class="button secondary" :disabled="busy||!localClient.connected" @click="check(true)"><AppIcon name="browser" :size="16"/>浏览器启动自检</button></div>
  <div class="capability-toolbar"><div class="filter-tabs"><button :class="{active:filter==='all'}" @click="filter='all'">全部</button><button :class="{active:filter==='attention'}" @click="filter='attention'">待处理</button></div><span class="muted">可选组件缺失不影响接口和 Web UI 测试</span></div>
  <div class="capability-grid"><article v-for="(item,index) in visibleChecks" :key="index" class="panel capability-card"><div class="resource-title"><h3>{{item.label}}</h3><span class="health-badge" :class="item.status"><i/>{{statusLabels[item.status]||item.status}}</span></div><p class="capability-detail">{{item.detail}}</p><span class="capability-location"><AppIcon name="terminal" :size="14"/>{{item.location}}</span><details class="capability-guide"><summary>检查指引<AppIcon name="down" :size="14"/></summary><div><a :href="item.docs" target="_blank" rel="noopener" class="text-button">官方文档</a><pre>{{item.command}}</pre><button class="text-button" @click="copy(item.command)">复制命令</button></div></details></article></div>
  <RouterLink to="/mobile" class="text-button capability-device-link">前往应用与设备检测 Appium 和 Android 连接<AppIcon name="arrow" :size="15"/></RouterLink>
 </section>
 <div v-else class="panel empty-state"><AppIcon name="web" :size="30"/><h3>执行服务就绪后显示检测结果</h3><p>可先管理项目、用例和历史报告；检查安装目录中的服务日志。</p></div>
</div>
</template>
