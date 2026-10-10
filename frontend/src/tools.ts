import type { IconName } from './components/Icon'
import type { Category, Tool } from './types/api'

export const TOOL_EMOJI: Record<string, string> = {
  '/tools/magnet-scraper': '🧲',
  '/tools/remux': '🎬',
  '/tools/torrent-downloader': '🌊',
  '/tools/web-images-to-pdf': '🌐',
  '/tools/file-gatherer': '📦',
  '/tools/image-to-pdf': '🖼️',
  '/tools/doc-to-pdf': '📄',
  '/tools/doc-to-markdown': '📝',
  '/tools/cache-purge': '🧹',
  '/tools/photos-library-filter': '📸',
  '/tools/subscription': '🛰️',
  '/tools/dep-upgrade': '📦',
}

export const TOOL_GROUPS = ['Media', 'Documents', 'Files', 'Network'] as const
export type ToolGroup = (typeof TOOL_GROUPS)[number]

interface ToolDetail {
  title: string
  group: ToolGroup
  icon: IconName
  tag: string
  description: string
}

export const TOOL_DETAILS: Record<string, ToolDetail> = {
  'magnet-scraper': {
    title: 'Magnet Scraper',
    group: 'Media',
    icon: 'magnet',
    tag: 'Collect & deduplicate',
    description: 'Collect magnet links in bulk, or tidy up a pasted list.',
  },
  remux: {
    title: 'Remux Processor',
    group: 'Media',
    icon: 'film',
    tag: 'Lossless processing',
    description: 'Repackage video and choose your tracks. Keep the original quality.',
  },
  'torrent-downloader': {
    title: 'Torrent Downloader',
    group: 'Media',
    icon: 'download',
    tag: 'BitComet',
    description: 'Choose the files you want and send them to your BitComet device.',
  },
  'web-images-to-pdf': {
    title: 'Web Images to PDF',
    group: 'Documents',
    icon: 'globe',
    tag: 'Chrome capture',
    description: 'Turn the images on a web page into one bookmarked PDF.',
  },
  'image-to-pdf': {
    title: 'Image to PDF',
    group: 'Documents',
    icon: 'image',
    tag: 'PNG · JPG · HEIC',
    description: 'Bring your images together in a single, ready-to-share PDF.',
  },
  'doc-to-pdf': {
    title: 'Doc to PDF',
    group: 'Documents',
    icon: 'file',
    tag: 'Word documents',
    description: 'Accept revisions, remove comments, and export clean PDFs.',
  },
  'doc-to-markdown': {
    title: 'Doc to Markdown',
    group: 'Documents',
    icon: 'markdown',
    tag: 'MinerU',
    description: 'Extract text, tables, and formulas into editable Markdown.',
  },
  'file-gatherer': {
    title: 'File Gatherer',
    group: 'Files',
    icon: 'folder',
    tag: 'Moves files',
    description: 'Find files by type in nested folders and move them into one place.',
  },
  'cache-purge': {
    title: 'Cache Purge',
    group: 'Files',
    icon: 'broom',
    tag: 'Review before deleting',
    description: 'Find cache and junk files. Review the list before clearing them.',
  },
  'photos-library-filter': {
    title: 'Photos Library Filter',
    group: 'Files',
    icon: 'photos',
    tag: 'Preview available',
    description: 'Create a lighter Photos library while preserving originals and edits.',
  },
  'dep-upgrade': {
    title: 'Dependency Upgrader',
    group: 'Files',
    icon: 'package',
    tag: 'Python · Node.js',
    description: 'Review package updates, upgrade your project, and commit changes.',
  },
  subscription: {
    title: 'Optimized-IP Subscription',
    group: 'Network',
    icon: 'network',
    tag: 'Proxy configurations',
    description: 'Apply preferred IPs to your nodes and generate subscription links.',
  },
}

export const GROUP_DESCRIPTIONS: Record<ToolGroup, string> = {
  Media: 'Collect, download, and process.',
  Documents: 'Convert, capture, and export.',
  Files: 'Organize, clean, and maintain.',
  Network: 'Configure and connect.',
}

// Keep the server manifest authoritative for enabled tools; grouping is presentation only.
export function groupTools(categories: Category[]): { name: ToolGroup; tools: Tool[] }[] {
  const tools = categories.flatMap((category) => category.tools)
  return TOOL_GROUPS.map((name) => ({
    name,
    tools: tools.filter((tool) => TOOL_DETAILS[tool.slug]?.group === name),
  })).filter((group) => group.tools.length > 0)
}

export function toolTitle(slug: string, fallback?: string): string {
  return (
    TOOL_DETAILS[slug]?.title ??
    fallback?.replace(/^\S+\s/, '') ??
    slug.replace(/-/g, ' ')
  )
}

// Keys are the category names the API returns.
export const CATEGORY_ACCENT: Record<string, string> = {
  '🎬 Media': 'var(--amber)',
  '🗂️ Files & Tools': 'var(--teal)',
  '🌐 Network': 'var(--violet)',
}
