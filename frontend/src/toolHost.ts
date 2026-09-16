// A hidden tool stays mounted but must stop working; pages gate their timers on useToolActive.

import { createContext, useContext, useEffect } from 'react'

export const ToolActiveContext = createContext(true)

/** True while this tool's tab is visible; true outside a host. */
export function useToolActive(): boolean {
  return useContext(ToolActiveContext)
}

export const ToolSlugContext = createContext('')

export const ToolBusyContext = createContext<(slug: string, busy: boolean) => void>(
  () => {},
)

/** Holds this tool's tab open while page-local async work is in flight. */
export function useToolBusy(busy: boolean): void {
  const slug = useContext(ToolSlugContext)
  const setBusy = useContext(ToolBusyContext)
  useEffect(() => {
    if (!slug) return
    setBusy(slug, busy)
    return () => setBusy(slug, false)
  }, [slug, busy, setBusy])
}
