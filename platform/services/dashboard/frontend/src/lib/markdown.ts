// Rewrites dashboard://image/<id> placeholders to actual presigned URLs.
// Called before rendering markdown so react-markdown can display uploaded images.
export function rewriteImageUrls(
  markdown: string,
  images: Array<{ id: string | null; placeholder: string; url: string }>,
): string {
  let result = markdown
  for (const img of images) {
    if (!img.id || !img.url) continue
    const placeholder = `dashboard://image/${img.id}`
    result = result.split(placeholder).join(img.url)
  }
  return result
}
