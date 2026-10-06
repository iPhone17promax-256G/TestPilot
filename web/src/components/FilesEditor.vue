<script setup lang="ts">
import {ref,watch} from 'vue'
const props=defineProps<{modelValue:Record<string,string>;title?:string;hint?:string;accept?:string}>()
const emit=defineEmits<{(e:'update:modelValue',value:Record<string,string>):void}>()
const rows=ref<{name:string;content:string}[]>([])
let last=''
watch(()=>props.modelValue,v=>{const value=JSON.stringify(v);if(value===last)return;last=value;rows.value=Object.entries(v).map(([name,content])=>({name,content}))},{immediate:true,deep:true})
function update(){const value=Object.fromEntries(rows.value.filter(r=>r.name.trim()).map(r=>[r.name.trim(),r.content]));last=JSON.stringify(value);emit('update:modelValue',value)}
async function upload(e:Event){for(const file of Array.from((e.target as HTMLInputElement).files||[])){const existing=rows.value.find(r=>r.name===file.name);if(existing)existing.content=await file.text();else rows.value.push({name:file.name,content:await file.text()})}update();(e.target as HTMLInputElement).value=''}
function remove(i:number){rows.value.splice(i,1);update()}
</script>
<template><div class="modal-body form-grid"><div class="step-section-heading"><strong>{{title||'版本辅助文件'}}</strong><div><button type="button" class="text-button" @click="rows.push({name:'',content:''})">＋ 新建文件</button><label class="text-button">上传文件<input type="file" :accept="accept||'.csv,.json,.txt,.py'" multiple hidden @change="upload"/></label></div></div><article v-for="(file,i) in rows" :key="i" class="key-value-editor"><div class="step-section-heading"><input v-model="file.name" :aria-label="'文件名称 '+(i+1)" placeholder="例如 data.csv" @input="update"/><button type="button" class="text-button danger" @click="remove(i)">删除</button></div><textarea v-model="file.content" :aria-label="'文件内容 '+(i+1)" rows="7" spellcheck="false" @input="update"/></article><p class="form-hint">{{hint||'支持 CSV、JSON、TXT 和 Python 辅助模块。文件与当前版本一起保存；JMeter CSV Data Set 使用同名文件。'}}</p></div></template>
