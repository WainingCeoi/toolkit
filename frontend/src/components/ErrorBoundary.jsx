// App-wide safety net: a render error in any tool page (or a malformed job
// snapshot) shows a recoverable message instead of a blank white screen.

import React from 'react'

export default class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props)
    this.state = { error: null }
  }

  static getDerivedStateFromError(error) {
    return { error }
  }

  render() {
    if (this.state.error) {
      return (
        <div className="note error" style={{ margin: 24 }}>
          <strong>Something went wrong.</strong>
          <div style={{ marginTop: 6, font: '12px var(--mono)' }}>
            {String(this.state.error?.message || this.state.error)}
          </div>
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
