import React from 'react'
import { createRoot } from 'react-dom/client'
import { createHashRouter, RouterProvider } from 'react-router'
import './styles.css'
import Layout from './Layout'
import { JobsProvider } from './JobsProvider'
import ErrorBoundary from './components/ErrorBoundary'

// Hash routing needs no server fallback; one catch-all route keeps open tools mounted.
const router = createHashRouter([{ path: '*', element: <Layout /> }])

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
