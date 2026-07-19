// Unlock overlay for LAN-hosted runs (make host). The API answers 401 until a
// valid token is presented; api.js fires a 'toolkit-auth-required' event on any
// 401, which surfaces this modal. Submitting stores the token (localStorage +
// cookie for the EventSource) and reloads so every initial fetch re-runs
// authenticated. On loopback dev/start the API never 401s, so this never shows.

import React, { useEffect, useState } from 'react'
import { setAuthToken } from '../api'
import Button from './Button'

export default function AuthGate() {
  const [open, setOpen] = useState(false)
  const [token, setToken] = useState('')

  useEffect(() => {
    const onRequired = () => setOpen(true)
    window.addEventListener('toolkit-auth-required', onRequired)
    return () => window.removeEventListener('toolkit-auth-required', onRequired)
  }, [])

  if (!open) return null

  function submit(e) {
    e.preventDefault()
    const value = token.trim()
    if (!value) return
    setAuthToken(value)
    window.location.reload()
  }

  return (
    <div className="auth-scrim" role="dialog" aria-modal="true" aria-labelledby="auth-title">
      <form className="auth-card" onSubmit={submit}>
        <h2 id="auth-title">🔑 Enter access token</h2>
        <p>
          This Toolkit is hosted on the LAN. Enter the access token printed in
          the server's terminal to unlock the tools.
        </p>
        <input
          className="control"
          type="password"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          placeholder="access token"
          autoFocus
          spellCheck={false}
        />
        <Button type="submit" variant="primary" disabled={!token.trim()}>
          Unlock
        </Button>
      </form>
    </div>
  )
}
