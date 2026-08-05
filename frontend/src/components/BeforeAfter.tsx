// Before/after reveal: the cleaned image sits on top of the original,
// clipped at a draggable divider. Plain <img> + CSS clip-path — no canvas.

import { useRef, useState } from 'react'

interface BeforeAfterProps {
  before: string
  after: string
  width: number
  height: number
}

export default function BeforeAfter({ before, after, width, height }: BeforeAfterProps) {
  const [pct, setPct] = useState(50)
  const container = useRef<HTMLDivElement>(null)
  const dragging = useRef(false)

  function setFromPointer(clientX: number) {
    const rect = container.current?.getBoundingClientRect()
    if (!rect || rect.width === 0) return
    setPct(Math.max(0, Math.min(100, ((clientX - rect.left) / rect.width) * 100)))
  }

  return (
    <div
      ref={container}
      className="ba"
      style={{ aspectRatio: `${width} / ${height}` }}
      onPointerDown={(e) => {
        dragging.current = true
        e.currentTarget.setPointerCapture(e.pointerId)
        setFromPointer(e.clientX)
      }}
      onPointerMove={(e) => dragging.current && setFromPointer(e.clientX)}
      onPointerUp={() => {
        dragging.current = false
      }}
    >
      <img src={before} alt="before" draggable={false} />
      {/* inset clips from the RIGHT, so pct% of the after-image shows. */}
      <div className="ba-after" style={{ clipPath: `inset(0 ${100 - pct}% 0 0)` }}>
        <img src={after} alt="after" draggable={false} />
      </div>
      <div className="ba-divider" style={{ left: `${pct}%` }} />
      <span className="ba-tag" style={{ right: 8 }}>
        before
      </span>
      <span className="ba-tag" style={{ left: 8 }}>
        after
      </span>
    </div>
  )
}
