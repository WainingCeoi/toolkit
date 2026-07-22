// Global job tracking: any tool can start a job here and its live snapshot
// stays available app-wide — the bottom dock renders every tracked job, so
// work keeps visibly running while you switch tools (the thing the old
// Streamlit app structurally could not do).

import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { followJob } from './api'
import type { Job, JobStarted } from './types/api'

/**
 * The provider is tool-agnostic, so it stores snapshots with an unknown result.
 * Each page re-attaches its own result type through useToolJob<R>() — that cast
 * is the single place a tool's result shape is claimed, instead of once per
 * field read.
 */
export type AnyJob = Job<unknown>

interface TrackedJob {
  snapshot: AnyJob
  toolPath: string
}

interface JobsContextValue {
  jobs: Record<string, TrackedJob>
  track: (jobId: string, toolPath: string) => Promise<AnyJob | null>
  dismiss: (jobId: string) => void
}

const JobsContext = createContext<JobsContextValue | null>(null)

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
      setJobs((prev) => ({ ...prev, [jobId]: { snapshot: normalizeSnapshot(snapshot), toolPath } }))
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
