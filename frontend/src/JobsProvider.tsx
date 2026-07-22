// The <JobsProvider> component, alone in its own module.
//
// It exports exactly one thing, and that thing is a component, which is what
// Fast Refresh requires to hot-swap a module instead of reloading it. The
// context, hooks, and types it builds on live in ./jobs.

import { useCallback, useRef, useState, type ReactNode } from 'react'
import { followJob } from './api'
import { JobsContext, type AnyJob, type TrackedJob } from './jobs'

// Defensive shape: an evicted-job SSE frame carries only { state }, so fill the
// fields the dock and panels read (items/message/result/error) before storing.
//
// The assertion is load-bearing and cannot be avoided: the input is whatever
// came off the wire, and no generic spread can prove to the checker that the
// filled object satisfies the state-discriminated union. This is the boundary
// where the wire shape is trusted; everything downstream is checked.
function normalizeSnapshot(snapshot: Partial<AnyJob>): AnyJob {
  return { items: [], message: '', result: null, error: null, ...snapshot } as AnyJob
}

export function JobsProvider({ children }: { children: ReactNode }) {
  const [jobs, setJobs] = useState<Record<string, TrackedJob>>({}) // id -> {snapshot, toolPath}
  const followed = useRef<Set<string>>(new Set())

  const track = useCallback((jobId: string, toolPath: string): Promise<AnyJob | null> => {
    if (followed.current.has(jobId)) return Promise.resolve(null)
    followed.current.add(jobId)
    const update = (snapshot: AnyJob) =>
      setJobs((prev) => ({
        ...prev,
        [jobId]: { snapshot: normalizeSnapshot(snapshot), toolPath },
      }))
    return followJob<unknown>(jobId, update).catch((err: Error) => {
      // A genuine failure (not a transient blip — followJob polls through
      // those): mark it failed and drop it from `followed` so it can be
      // re-tracked later.
      followed.current.delete(jobId)
      setJobs((prev) => {
        const cur = prev[jobId]
        if (!cur) return prev
        const snap = { ...cur.snapshot, state: 'failed', error: err.message } as AnyJob
        return { ...prev, [jobId]: { ...cur, snapshot: snap } }
      })
      throw err
    })
  }, [])

  const dismiss = useCallback((jobId: string) => {
    followed.current.delete(jobId)
    setJobs((prev) => {
      const next = { ...prev }
      delete next[jobId]
      return next
    })
  }, [])

  return <JobsContext.Provider value={{ jobs, track, dismiss }}>{children}</JobsContext.Provider>
}
