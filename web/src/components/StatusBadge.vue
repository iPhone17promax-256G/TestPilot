<script setup lang="ts">
import {statusLabels} from '../uiHelpers'
import AppIcon from './AppIcon.vue'
defineProps<{ status: string; label?: string }>()
const labels:Record<string,string>={...statusLabels,pending:'待确认',needs_input:'待补充',budget_exhausted:'预算耗尽'}
const icons: Record<string, string> = { passed: 'passed', failed: 'failed', running: 'running', cancelled: 'pause', error: 'failed', completed: 'checks', draft: 'circle', pending: 'clock' }
</script>
<template><span class="status-badge" :class="status"><AppIcon :name="icons[status] ?? 'circle'" :size="13" />{{ label ?? labels[status] ?? status }}</span></template>
