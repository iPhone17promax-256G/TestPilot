<script setup lang="ts">
import {ref} from 'vue'
import type {ToolDecision} from '../types'
import {decideApproval,notify} from '../store'
const props=defineProps<{approval:ToolDecision;taskId:string}>()
const busy=ref(false),reason=ref('')
const labels:Record<string,string>={pending:'等待你的确认',allowed:'允许执行',approved:'你已批准',rejected:'你已拒绝',denied:'拒绝执行',stale:'旧操作已失效',executing:'正在执行',executed:'已执行',error:'执行失败'}
async function decide(decision:'approve'|'reject'){if(busy.value)return;busy.value=true;try{await decideApproval(props.taskId,props.approval.id,decision,reason.value)}catch(e){notify(String(e))}finally{busy.value=false}}
</script>
<template><section class="tool-approval" :class="{pending:approval.status==='pending'}" aria-label="工具权限判断"><strong>{{approval.mode==='auto'?'AI 权限判断':approval.mode==='readonly'?'只读权限':'免确认权限'}} · {{labels[approval.status]||approval.status}}</strong><p>{{approval.reason}}</p><p v-if="approval.resolutionReason" class="muted">{{approval.resolutionReason}}</p><template v-if="approval.status==='pending'"><p class="form-hint">只批准本次调用和当前参数。你也可以直接发新消息调整要求，旧操作会取消。</p><input v-model="reason" aria-label="审批补充说明" maxlength="1000" placeholder="补充说明（可选，例如：先别执行）"/><div class="approval-actions"><button type="button" class="button primary" :disabled="busy||!approval.interruptId" @click="decide('approve')">批准这一次</button><button type="button" class="button secondary" :disabled="busy||!approval.interruptId" @click="decide('reject')">拒绝</button><span v-if="!approval.interruptId" class="muted">正在保存暂停进度…</span></div></template></section></template>
