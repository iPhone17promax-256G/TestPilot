import MarkdownIt from 'markdown-it'

const markdown = new MarkdownIt({ html: false, breaks: true })
// Image labels come from untrusted model/document text. This custom renderer
// must escape them just as MarkdownIt's built-in text renderer does.
markdown.renderer.rules.image = (tokens, index) =>
  markdown.utils.escapeHtml(tokens[index]?.content || '[图片]')

export function renderMarkdown(content: string): string {
  return markdown.render(content || '')
}
