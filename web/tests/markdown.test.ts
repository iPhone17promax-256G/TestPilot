import assert from 'node:assert/strict'
import test from 'node:test'
import { renderMarkdown } from '../src/markdown.ts'

test('image alt text cannot inject HTML into model replies', () => {
  const html = renderMarkdown('![<img src=x onerror=alert(1)>](https://example.test/image.png)')
  assert.ok(html.includes('&lt;img src=x onerror=alert(1)&gt;'))
  assert.ok(!html.includes('<img'))
})

test('raw HTML and javascript links stay inactive, ordinary Markdown works', () => {
  const html = renderMarkdown('<script>alert(1)</script>\n\n[bad](javascript:alert(1))\n\n**结果**\n\n[报告](/runs/RUN-1)')
  assert.ok(!html.includes('<script>'))
  assert.ok(!html.includes('href="javascript:'))
  assert.ok(html.includes('<strong>结果</strong>'))
  assert.ok(html.includes('href="/runs/RUN-1"'))
})
