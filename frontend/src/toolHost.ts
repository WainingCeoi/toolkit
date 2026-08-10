// Keep-alive means MOUNTED, not active: a background tab's page keeps its
// state but must not keep working. Pages with their own timers (status
// polls) gate on this so a hidden tool stops hitting the backend.

import { createContext, useContext } from 'react'

export const ToolActiveContext = createContext(true)

/** True while this page's tab is the visible one. Defaults true so a page
 *  rendered outside a tool host (tests, storybook-style harnesses) behaves
 *  like the old always-visible lifecycle. */
export function useToolActive(): boolean {
  return useContext(ToolActiveContext)
}
