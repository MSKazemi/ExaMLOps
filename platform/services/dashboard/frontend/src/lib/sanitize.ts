// Frontend content sanitization — defense-in-depth for user/authored markdown (F16 R2 / ADR 0053).
//
// The dashboard renders markdown (Docs, model READMEs) via `react-markdown`, which does NOT render
// raw HTML unless `rehype-raw` is added — so injected markup is already inert. These helpers are a
// *belt-and-suspenders* layer: they strip the common XSS vectors from a markdown string before it is
// rendered, and provide a URL guard for any place that ever builds an href/src from user input.
//
// If a future feature needs to render raw HTML, route it through a vetted allowlist sanitizer
// (DOMPurify / rehype-sanitize) rather than relaxing these — never introduce unsanitized
// `dangerouslySetInnerHTML` (lint-enforced, F16 R2).

const SCRIPT_BLOCK = /<script\b[^>]*>[\s\S]*?<\/script\s*>/gi
const STYLE_BLOCK = /<style\b[^>]*>[\s\S]*?<\/style\s*>/gi
const DANGEROUS_TAGS = /<\/?(?:iframe|object|embed|link|meta|base)\b[^>]*>/gi
// Inline event handlers: on<name>=... (quoted or bare).
const EVENT_HANDLER = /\son[a-z]+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)/gi
// Script-ish URI schemes anywhere in an attribute value. Two variants on purpose:
// the global one is for `.replace` (strip every occurrence); the non-global one is
// for `.test` in `safeUrl`. Sharing a single `g`-flagged regex between `.replace`
// and `.test` is unsafe — `.test` advances `lastIndex`, so a shared instance can
// start mid-string on the next call and miss a match.
const SCRIPT_URI_GLOBAL = /(?:javascript|vbscript|data:text\/html)\s*:/gi
const SCRIPT_URI = /(?:javascript|vbscript|data:text\/html)\s*:/i

/**
 * Strip common XSS vectors from a markdown/HTML string (F16 R2). Removes `<script>`/`<style>`
 * blocks, framing/`link`/`meta` tags, inline `on*=` event handlers, and neutralizes
 * `javascript:` / `data:text/html` URIs. Idempotent and safe on plain markdown (no-op).
 */
export function sanitizeMarkdown(input: string): string {
  if (!input) return ''
  return input
    .replace(SCRIPT_BLOCK, '')
    .replace(STYLE_BLOCK, '')
    .replace(DANGEROUS_TAGS, '')
    .replace(EVENT_HANDLER, '')
    .replace(SCRIPT_URI_GLOBAL, '')
}

/**
 * Return a safe href/src, or `''` if the URL uses a script-ish scheme (F16 R2). Allows relative
 * URLs and the usual safe schemes (http/https/mailto and image data URIs).
 */
export function safeUrl(url: string): string {
  const trimmed = (url ?? '').trim()
  if (!trimmed) return ''
  // SCRIPT_URI is non-global, so `.test` is stateless (no lastIndex carry-over).
  if (SCRIPT_URI.test(trimmed)) return ''
  return trimmed
}
