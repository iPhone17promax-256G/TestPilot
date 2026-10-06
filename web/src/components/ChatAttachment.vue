<script setup lang="ts">
import {ref} from 'vue'
import type {ChatAttachment} from '../types'
import AppIcon from './AppIcon.vue'
import AppModal from './AppModal.vue'
import {request,notify} from '../store'
const props=defineProps<{attachment:ChatAttachment;removable?:boolean}>()
defineEmits<{remove:[]}>()
const open=ref(false),loading=ref(false),content=ref(''),nextOffset=ref<number|null>(null),error=ref('')
async function load(offset=0){loading.value=true;error.value='';try{const result=await request<{content:string;nextOffset:number|null}>('/attachments/'+props.attachment.id+'/text?offset='+offset);content.value=offset?content.value+result.content:result.content;nextOffset.value=result.nextOffset}catch(e){error.value=String(e);notify(String(e))}finally{loading.value=false}}
function preview(){content.value='';nextOffset.value=null;open.value=true;void load()}
</script>
<template>
<div class="chat-attachment">
<AppIcon name="file" :size="19"/>
<div><button type="button" class="attachment-name" @click="preview">{{attachment.name}}</button><small>{{attachment.type.toUpperCase()}} · {{(attachment.size/1024).toFixed(1)}} KB · 已解析 {{attachment.characters.toLocaleString()}} 字</small><span v-if="attachment.warnings.length" class="attachment-warning">解析提醒，请预览核对</span></div>
<button v-if="removable" type="button" class="icon-button" :aria-label="'移除附件 '+attachment.name" @click="$emit('remove')"><AppIcon name="close" :size="14"/></button>
</div>
<AppModal :open="open" :title="attachment.name" @close="open=false"><div class="modal-body attachment-preview"><p class="form-hint">以下是 Agent 可以读取的正文；原文 {{attachment.characters.toLocaleString()}} 字{{attachment.pages?' · '+attachment.pages+' 页':''}}。</p><p v-for="warning in attachment.warnings" :key="warning" class="attachment-warning">{{warning}}</p><pre v-if="content">{{content}}</pre><p v-if="error" class="attachment-warning">{{error}}</p><p v-if="loading" class="form-hint">正在读取正文…</p><button v-if="nextOffset!==null" type="button" class="button secondary" :disabled="loading" @click="load(nextOffset!)">继续读取正文</button></div><div class="modal-actions"><a :href="attachment.downloadUrl" class="button secondary" download>下载原文件</a><button type="button" class="button primary" @click="open=false">关闭预览</button></div></AppModal>
</template>
