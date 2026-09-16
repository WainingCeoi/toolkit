import { useState } from 'react'
import { copyText } from '../clipboard'

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
