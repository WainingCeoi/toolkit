// App-wide safety net: a render error in any tool page (or a malformed job
// snapshot) shows a recoverable message instead of a blank white screen.

import React, { type ReactNode } from 'react'

interface ErrorBoundaryProps {
  children: ReactNode
}

// `unknown`, not Error: React re-throws whatever was thrown, and a page can
// throw a non-Error (a string, a rejected value). The render path below already
// handled that with `error?.message || error`.
interface ErrorBoundaryState {
  error: unknown
}

export default class ErrorBoundary extends React.Component<
  ErrorBoundaryProps,
  ErrorBoundaryState
> {
  constructor(props: ErrorBoundaryProps) {
    super(props)
    this.state = { error: null }
  }

  static getDerivedStateFromError(error: unknown): ErrorBoundaryState {
    return { error }
  }

  render() {
    if (this.state.error) {
      const { error } = this.state
      const message = error instanceof Error ? error.message : String(error)
      return (
        <div className="note error" style={{ margin: 24 }}>
          <strong>Something went wrong.</strong>
          <div style={{ marginTop: 6, font: '12px var(--mono)' }}>{message}</div>
          <button
            type="button"
            className="btn"
            style={{ marginTop: 12 }}
            onClick={() => window.location.reload()}
          >
            Reload
          </button>
        </div>
      )
    }
    return this.props.children
  }
}
