<script setup lang="ts">
import {ref,watch} from 'vue'
const props=defineProps<{modelValue:Record<string,any>;label:string}>()
const emit=defineEmits<{(e:'update:modelValue',value:Record<string,string>):void}>()
const rows=ref<{key:string;value:string}[]>([])
let last=''
watch(()=>props.modelValue,v=>{const text=JSON.stringify(v||{});if(text===last)return;last=text;rows.value=Object.entries(v||{}).map(([key,value])=>({key,value:String(value)}))},{immediate:true,deep:true})
function update(){const value=Object.fromEntries(rows.value.filter(r=>r.key.trim()).map(r=>[r.key.trim(),r.value]));last=JSON.stringify(value);emit('update:modelValue',value)}
function remove(i:number){rows.value.splice(i,1);update()}
</script>
<template><div class="key-value-editor"><div class="step-section-heading"><strong>{{label}}</strong><button class="text-button" type="button" @click="rows.push({key:'',value:''})">＋ 添加</button></div><div v-for="(r,i) in rows" :key="i" class="key-value-row"><input v-model="r.key" :aria-label="label+'名称 '+(i+1)" placeholder="名称" @input="update"/><input v-model="r.value" :aria-label="label+'值 '+(i+1)" placeholder="值，可引用 {{变量}}" @input="update"/><button class="text-button danger" type="button" :aria-label="'删除'+label+' '+(i+1)" @click="remove(i)">删除</button></div><p v-if="!rows.length" class="form-hint">尚未配置</p></div></template>
