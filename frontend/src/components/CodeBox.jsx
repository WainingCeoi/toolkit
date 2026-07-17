// Fixed-height, copyable code block (the old pages' st.code contract).

import React, { useState } from 'react'

export default function CodeBox({ text }) {
  const [copied, setCopied] = useState(false)

  async function copy() {
    await navigator.clipboard.writeText(text)
    setCopied(true)
    setTimeout(() => setCopied(false), 1200)
  }

  return (
    <div className="codebox">
      <button type="button" className="copybtn" onClick={copy}>
        {copied ? 'copied' : 'copy'}
      </button>
      {text}
    </div>
  )
}
