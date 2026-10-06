<script setup lang="ts">
import {computed} from 'vue'
import {renderMarkdown} from '../markdown'
const props=defineProps<{content:string}>()
const html=computed(()=>renderMarkdown(props.content))
</script>
<template><div class="markdown-text" v-html="html"/></template>
