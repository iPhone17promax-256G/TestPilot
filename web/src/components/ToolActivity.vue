<script setup lang="ts">
import {computed} from 'vue'
import type {AgentTask,ToolDecision} from '../types'
import ToolApproval from './ToolApproval.vue'
import AppIcon from './AppIcon.vue'
import {toolLabel,eventOutcome,errorSummary} from '../uiHelpers'
const props=defineProps<{event:AgentTask['events'][number];approval?:ToolDecision;taskId?:string}>()
function format(value:string){try{return JSON.stringify(JSON.parse(value),null,2)}catch{return value}}
const title=computed(()=>{if(props.event.name.startsWith('mcp_')){try{const result=JSON.parse(props.event.result);if(result.server&&result.tool)return result.server+' / '+result.tool}catch{}}return toolLabel(props.event.name)})
const failure=computed(()=>props.event.status==='error'?errorSummary(props.event.result):'')
</script>
<template><details class="chat-tool" :class="event.status" :open="['error','waiting_approval','blocked'].includes(event.status||'')"><summary><AppIcon :name="event.status==='running'?'running':['error','blocked'].includes(event.status||'')?'failed':event.status==='waiting_approval'?'terminal':'check'" :size="16"/><strong>{{title}}</strong><span class="tool-outcome">{{eventOutcome(event)}}</span><AppIcon name="down" :size="13"/></summary><div class="tool-content"><p v-if="failure" class="inline-error">{{failure}}</p><code class="muted">{{event.name}}</code><details :open="event.status==='waiting_approval'"><summary>调用参数</summary><pre>{{format(approval?.arguments||event.arguments)}}</pre></details><ToolApproval v-if="approval&&taskId" :approval="approval" :task-id="taskId"/><details><summary>返回结果</summary><pre>{{format(event.result)}}</pre></details></div></details></template>
