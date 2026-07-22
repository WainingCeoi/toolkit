// Fixed-height, copyable code block (the old pages' st.code contract).

import { useState } from 'react'

// navigator.clipboard only exists in secure contexts (https / localhost), so it
// is undefined when the app is opened over plain HTTP on the LAN (make host).
// Fall back to a hidden-textarea execCommand copy there, and surface failure
// instead of throwing silently.
async function copyText(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text)
    return
  }
  const ta = document.createElement('textarea')
  ta.value = text
  ta.style.position = 'fixed'
  ta.style.opacity = '0'
  document.body.appendChild(ta)
  ta.select()
  try {
    if (!document.execCommand('copy')) throw new Error('copy rejected')
  } finally {
    ta.remove()
  }
}

type CopyStatus = 'idle' | 'copied' | 'failed'

export default function CodeBox({ text }: { text: string }) {
  const [status, setStatus] = useState<CopyStatus>('idle')

  async function copy() {
    try {
      await copyText(text)
      setStatus('copied')
    } catch {
      setStatus('failed')
    }
    setTimeout(() => setStatus('idle'), 1500)
  }

  const label = status === 'copied' ? 'copied' : status === 'failed' ? 'select & copy' : 'copy'

  return (
    <div className="codebox">
      <button type="button" className="copybtn" onClick={copy}>
        {label}
      </button>
      {text}
    </div>
  )
}
