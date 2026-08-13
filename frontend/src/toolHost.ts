// Keep-alive means MOUNTED, not active: a background tab's page keeps its
// state but must not keep working. Pages with their own timers (status
// polls) gate on this so a hidden tool stops hitting the backend.

import { createContext, useContext, useEffect } from 'react'

export const ToolActiveContext = createContext(true)

/** True while this page's tab is the visible one. Defaults true so a page
 *  rendered outside a tool host (tests, storybook-style harnesses) behaves
 *  like the old always-visible lifecycle. */
export function useToolActive(): boolean {
  return useContext(ToolActiveContext)
}

/** Which tool the surrounding host renders. Empty outside one. */
export const ToolSlugContext = createContext('')

/** Stable registry callback: both values it needs are passed in, so nothing
 *  here has to be rebuilt per host and per render. */
export const ToolBusyContext = createContext<(slug: string, busy: boolean) => void>(
  () => {},
)

/**
 * Hold this tool's tab open while page-local work is in flight.
 *
 * The dock refuses to close a tab whose job is still running, but it can only
 * see jobs in the registry. Work a page runs in its own async loops — Torrent
 * Downloader resolves and sends in component closures — is invisible to it, so
 * the tab closed freely and the loops carried on detached: staging magnets
 * into BitComet with every state update landing on an unmounted page.
 */
export function useToolBusy(busy: boolean): void {
  const slug = useContext(ToolSlugContext)
  const setBusy = useContext(ToolBusyContext)
  useEffect(() => {
    if (!slug) return
    setBusy(slug, busy)
    return () => setBusy(slug, false)
  }, [slug, busy, setBusy])
}
