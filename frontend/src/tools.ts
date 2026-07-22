// Route path -> emoji, used by the dock; category accents for the home grid.

// Indexed by arbitrary route path (the dock looks up whatever route is
// active), so a plain string key — not a union of the literals below.
export const TOOL_EMOJI: Record<string, string> = {
  '/tools/magnet-scraper': '🧲',
  '/tools/remux': '🎬',
  '/tools/web-images-to-pdf': '🌐',
  '/tools/file-gatherer': '📦',
  '/tools/image-to-pdf': '🖼️',
  '/tools/doc-to-pdf': '📄',
  '/tools/doc-to-markdown': '📝',
  '/tools/cache-purge': '🧹',
  '/tools/subscription': '🛰️',
  '/tools/dep-upgrade': '📦',
}

// Likewise keyed by the category name the API returns.
export const CATEGORY_ACCENT: Record<string, string> = {
  '🎬 Media': 'var(--amber)',
  '🗂️ Files & Tools': 'var(--teal)',
  '🌐 Network': 'var(--violet)',
}
