import React, { Suspense, lazy } from 'react'
import { createRoot } from 'react-dom/client'
import { createHashRouter, RouterProvider } from 'react-router-dom'
import './styles.css'
import Layout from './Layout'
import Home from './Home'
import { JobsProvider } from './jobs'
import AuthGate from './components/AuthGate'
import ErrorBoundary from './components/ErrorBoundary'
import { getAuthToken, setAuthToken } from './api'

// Re-affirm the auth cookie from a stored token on boot, so the same-origin
// EventSource carries it even if only localStorage survived (no-op when unset).
const savedToken = getAuthToken()
if (savedToken) setAuthToken(savedToken)

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

createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <ErrorBoundary>
      <JobsProvider>
        <RouterProvider router={router} />
        <AuthGate />
      </JobsProvider>
    </ErrorBoundary>
  </React.StrictMode>,
)
