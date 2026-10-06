<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref, watch } from 'vue'
import { EditorView, basicSetup } from 'codemirror'
import { EditorState } from '@codemirror/state'
import { xml } from '@codemirror/lang-xml'
import { python } from '@codemirror/lang-python'
const props = withDefaults(defineProps<{ modelValue: string; readonly?: boolean;language?:string;highlight?:{start:number;end:number} }>(), { readonly: false,language:"python" })
const emit = defineEmits<{ 'update:modelValue': [value: string] }>()
const element = ref<HTMLDivElement>()
let editor: EditorView | undefined
onMounted(() => {
  editor = new EditorView({ parent: element.value, doc: props.modelValue, extensions: [
    basicSetup, props.language==="xml"?xml():python(), EditorView.editable.of(!props.readonly), EditorState.readOnly.of(props.readonly),
    EditorView.contentAttributes.of({ 'aria-label': props.language==='xml'?'JMX 测试计划':'Python 用例代码' }),
    EditorView.updateListener.of(update => { if (update.docChanged) emit('update:modelValue', update.state.doc.toString()) }),
    EditorView.theme({ '&': { fontSize: '13px', minHeight: '320px' }, '.cm-content': { fontFamily: '"SFMono-Regular", Consolas, "Liberation Mono", monospace', padding: '16px 0' }, '.cm-line': { padding: '0 18px', lineHeight: '1.85' }, '.cm-gutters': { backgroundColor: '#f8faf9', borderRight: '1px solid #edf0ee', color: '#9ba5a1', paddingTop: '16px' }, '.cm-activeLine': { backgroundColor: '#f3f8f5' }, '&.cm-focused': { outline: 'none' }, '.cm-scroller': { overflow: 'auto' } }),
  ] })
  highlight()
})
function highlight(){
  if(!editor||!props.highlight)return
  const doc=editor.state.doc,{start,end}=props.highlight
  if(start<1||end>doc.lines)return
  const from=doc.line(start).from,to=doc.line(end).to
  editor.dispatch({selection:{anchor:from,head:to},effects:EditorView.scrollIntoView(from,{y:'center'})})
}
watch(()=>props.highlight,highlight,{deep:true})
watch(() => props.modelValue, value => {
  if (editor && value !== editor.state.doc.toString()) editor.dispatch({ changes: { from: 0, to: editor.state.doc.length, insert: value } })
  highlight()
})
onBeforeUnmount(() => editor?.destroy())
</script>
<template><div ref="element" class="code-editor" /></template>
