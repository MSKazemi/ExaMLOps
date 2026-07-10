// Skip-to-content link (F18 / ADR 0068, R2). Visually hidden until focused, so keyboard users can jump
// straight past the nav to the main region. Pairs with `<main id="main" tabIndex={-1}>` in Layout.
export function SkipLink() {
  return (
    <a
      href="#main"
      className="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-[100] focus:rounded-md focus:border focus:border-border focus:bg-background focus:px-3 focus:py-2 focus:text-sm focus:shadow-lg"
    >
      Skip to main content
    </a>
  )
}
