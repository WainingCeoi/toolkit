import React from 'react'
import { createRoot } from 'react-dom/client'
import { createHashRouter, RouterProvider } from 'react-router'
import './styles.css'
import Layout from './Layout'
import { JobsProvider } from './JobsProvider'
import ErrorBoundary from './components/ErrorBoundary'

// Hash routing keeps deep links working under the single-origin static mount
// without any server-side fallback config. One catch-all route: Layout maps
// the location to a page itself, because a route table would unmount a page
// on every navigation — and open tools must stay mounted (hidden) so their
// half-configured state survives switching. The registry lives in pages.ts.
const router = createHashRouter([{ path: '*', element: <Layout /> }])

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
