// Global job tracking: any tool can start a job here and its live snapshot
// stays available app-wide — the bottom dock renders every tracked job, so
// work keeps visibly running while you switch tools (the thing the old
// Streamlit app structurally could not do).
//
// The context, its types, and the consumer hooks live here; <JobsProvider>
// lives in its own module. Splitting them is what lets Fast Refresh work: a
// module exporting both a component and other values is fully reloaded on every
// edit instead of hot-swapped, which would drop the in-flight job state this
// module exists to preserve.

import { createContext, useCallback, useContext, useMemo, useState } from 'react'
import type { Job, JobStarted } from './types/api'

/**
 * The provider is tool-agnostic, so it stores snapshots with an unknown result.
 * Each page re-attaches its own result type through useToolJob<R>() — that cast
 * is the single place a tool's result shape is claimed, instead of once per
 * field read.
 */
export type AnyJob = Job<unknown>

export interface TrackedJob {
  snapshot: AnyJob
  toolPath: string
}

export interface JobsContextValue {
  jobs: Record<string, TrackedJob>
  track: (jobId: string, toolPath: string) => Promise<AnyJob | null>
  dismiss: (jobId: string) => void
}

export const JobsContext = createContext<JobsContextValue | null>(null)

export function useJobs(): JobsContextValue {
  const ctx = useContext(JobsContext)
  // Previously this returned null and the caller destructured it, which would
  // have thrown a less obvious "cannot read property of null" at render time.
  if (!ctx) throw new Error('useJobs must be used inside a <JobsProvider>')
  return ctx
}

interface ToolJob<R> {
  start: (startFn: () => Promise<JobStarted>) => Promise<string | null>
  snapshot: Job<R> | null
  running: boolean
  error: string | null
  setError: (message: string | null) => void
}

// Convenience for tool pages: start + track + expose the live snapshot of
// the page's own most recent job. R is the tool's result shape — naming it
// here is what makes every `snapshot.result.…` read in the page checked.
export function useToolJob<R>(toolPath: string): ToolJob<R> {
  const { jobs, track } = useJobs()
  const [jobId, setJobId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const start = useCallback(
    async (startFn: () => Promise<JobStarted>): Promise<string | null> => {
      setError(null)
      try {
        const { job_id: id } = await startFn()
        setJobId(id)
        track(id, toolPath).catch((err: Error) => setError(err.message))
        return id
      } catch (err) {
        setError((err as Error).message)
        return null
      }
    },
    [track, toolPath],
  )

  // Fall back to the most recent tracked job for this tool when the local id is
  // gone (component-local state is lost on unmount, but the provider keeps the
  // snapshot). This keeps a running job visible — and its Start button
  // disabled — after navigating away and back, so it can't be launched twice.
  const contextId = useMemo(() => {
    const ids = Object.keys(jobs).filter((id) => jobs[id]?.toolPath === toolPath)
    return ids.length ? ids[ids.length - 1] : null
  }, [jobs, toolPath])
  const activeId = jobId && jobs[jobId] ? jobId : contextId

  const snapshot = (activeId ? (jobs[activeId]?.snapshot ?? null) : null) as Job<R> | null
  const running = snapshot ? snapshot.state === 'running' : false
  return { start, snapshot, running, error, setError }
}
