import React, { Suspense, lazy } from 'react'
import { createRoot } from 'react-dom/client'
import { createHashRouter, RouterProvider } from 'react-router-dom'
import './styles.css'
import Layout from './Layout'
import Home from './Home'
import { JobsProvider } from './JobsProvider'
import ErrorBoundary from './components/ErrorBoundary'

// Hash routing keeps deep links working under the single-origin static mount
// without any server-side fallback config.
const pages = {
  'magnet-scraper': lazy(() => import('./pages/MagnetScraper')),
  remux: lazy(() => import('./pages/Remux')),
  'web-images-to-pdf': lazy(() => import('./pages/WebImagesToPdf')),
  'file-gatherer': lazy(() => import('./pages/FileGatherer')),
  'image-to-pdf': lazy(() => import('./pages/ImageToPdf')),
  'doc-to-pdf': lazy(() => import('./pages/DocToPdf')),
  'doc-to-markdown': lazy(() => import('./pages/DocToMarkdown')),
  'cache-purge': lazy(() => import('./pages/CachePurge')),
  subscription: lazy(() => import('./pages/Subscription')),
  'dep-upgrade': lazy(() => import('./pages/DepUpgrade')),
}

const router = createHashRouter([
  {
    element: <Layout />,
    children: [
      { path: '/', element: <Home /> },
      ...Object.entries(pages).map(([slug, Page]) => ({
        path: `/tools/${slug}`,
        element: (
          <Suspense fallback={<div className="note info">Loading…</div>}>
            <Page />
          </Suspense>
        ),
      })),
    ],
  },
])

// index.html always contains #root; a missing one is a build-time mistake, and
// the JS version would have thrown the same way one line later.
const rootElement = document.getElementById('root')
if (!rootElement) throw new Error('#root is missing from index.html')

createRoot(rootElement).render(
  <React.StrictMode>
    <ErrorBoundary>
      <JobsProvider>
        <RouterProvider router={router} />
      </JobsProvider>
    </ErrorBoundary>
  </React.StrictMode>,
)
