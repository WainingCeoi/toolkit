import { lazy } from 'react'

export const PAGES = {
  'magnet-scraper': lazy(() => import('./pages/MagnetScraper')),
  remux: lazy(() => import('./pages/Remux')),
  'torrent-downloader': lazy(() => import('./pages/TorrentDownloader')),
  'web-images-to-pdf': lazy(() => import('./pages/WebImagesToPdf')),
  'file-gatherer': lazy(() => import('./pages/FileGatherer')),
  'image-to-pdf': lazy(() => import('./pages/ImageToPdf')),
  'watermark-remover': lazy(() => import('./pages/WatermarkRemover')),
  'doc-to-pdf': lazy(() => import('./pages/DocToPdf')),
  'doc-to-markdown': lazy(() => import('./pages/DocToMarkdown')),
  'cache-purge': lazy(() => import('./pages/CachePurge')),
  'photos-library-filter': lazy(() => import('./pages/PhotosLibraryFilter')),
  subscription: lazy(() => import('./pages/Subscription')),
  'dep-upgrade': lazy(() => import('./pages/DepUpgrade')),
}

export type ToolSlug = keyof typeof PAGES

export function isToolSlug(slug: string): slug is ToolSlug {
  return slug in PAGES
}
