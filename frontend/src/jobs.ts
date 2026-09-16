// Jobs context and hooks. The provider is a separate module so Fast Refresh can hot-swap it.

import { createContext, useCallback, useContext, useMemo, useState } from 'react'
import type { Job, JobStarted } from './types/api'

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

export function useToolJob<R>(toolPath: string): ToolJob<R> {
  const { jobs, track } = useJobs()
  const [jobId, setJobId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  // Gates Start from the click until track() settles; no snapshot exists before the first frame.
  const [starting, setStarting] = useState(false)

  const start = useCallback(
    async (startFn: () => Promise<JobStarted>): Promise<string | null> => {
      setError(null)
      setStarting(true)
      let id: string
      try {
        id = (await startFn()).job_id
      } catch (err) {
        setError((err as Error).message)
        setStarting(false)
        return null
      }
      setJobId(id)
      track(id, toolPath)
        .catch((err: Error) => setError(err.message))
        .finally(() => setStarting(false))
      return id
    },
    [track, toolPath],
  )

  // Newest by created_at, not map order: re-attached jobs land in probe-completion order.
  const contextId = useMemo(() => {
    const mine = Object.keys(jobs).filter((id) => jobs[id]?.toolPath === toolPath)
    if (mine.length === 0) return null
    return mine.reduce((newest, id) =>
      (jobs[id]?.snapshot.created_at ?? '') > (jobs[newest]?.snapshot.created_at ?? '')
        ? id
        : newest,
    )
  }, [jobs, toolPath])
  const activeId = jobId && jobs[jobId] ? jobId : contextId

  const snapshot = (activeId ? (jobs[activeId]?.snapshot ?? null) : null) as Job<R> | null
  const running = starting || (snapshot ? snapshot.state === 'running' : false)
  return { start, snapshot, running, error, setError }
}
