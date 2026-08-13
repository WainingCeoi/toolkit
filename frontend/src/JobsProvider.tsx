// The <JobsProvider> component, alone in its own module.
//
// It exports exactly one thing, and that thing is a component, which is what
// Fast Refresh requires to hot-swap a module instead of reloading it. The
// context, hooks, and types it builds on live in ./jobs.

import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { api, followJob } from './api'
import { JobsContext, type AnyJob, type TrackedJob } from './jobs'
import { readSessionArray, writeSession } from './sessionStore'

// Tracked job ids survive a reload alongside the open-tab list. Job tracking
// is otherwise in-memory only, so refreshing mid-run used to orphan the job
// outright: no dock chip, a tab that closed without protest, and a Start
// button that happily launched a duplicate while the first one kept working.
const TRACKED_KEY = 'toolkit.trackedJobs'

interface StoredJob {
  id: string
  toolPath: string
}

function restorableJobs(): StoredJob[] {
  return readSessionArray(TRACKED_KEY).filter(
    (entry): entry is StoredJob =>
      typeof entry === 'object' &&
      entry !== null &&
      typeof (entry as StoredJob).id === 'string' &&
      typeof (entry as StoredJob).toolPath === 'string',
  )
}

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

  // Ids that must no longer be held in storage: dismissed by the user, or
  // found to be gone when we tried to re-attach.
  const retired = useRef<Set<string>>(new Set())

  const dismiss = useCallback((jobId: string) => {
    followed.current.delete(jobId)
    retired.current.add(jobId)
    setJobs((prev) => {
      const next = { ...prev }
      delete next[jobId]
      return next
    })
  }, [])

  // Re-attach to whatever was being tracked before the reload. Probed first
  // rather than tracked blind: the registry keeps only the most recent jobs,
  // and re-following an evicted one would raise a "failed" chip for work that
  // actually finished. A job still in the registry comes back whole — live
  // progress if it is running, its result and download if it already finished.
  const restorable = useRef<StoredJob[] | null>(null)
  restorable.current ??= restorableJobs()
  useEffect(() => {
    let cancelled = false
    for (const { id, toolPath } of restorable.current ?? []) {
      api
        .job(id)
        .then(() => {
          if (!cancelled) void track(id, toolPath).catch(() => {})
        })
        .catch(() => {
          retired.current.add(id) // evicted or unreachable
        })
    }
    return () => {
      cancelled = true
    }
  }, [track])

  // A stored id is kept until it is either tracked or retired, so the first
  // commit — when `jobs` is still empty — cannot erase the list the restore
  // pass above is only just working through.
  useEffect(() => {
    const live = Object.entries(jobs).map(([id, { toolPath }]) => ({ id, toolPath }))
    const liveIds = new Set(live.map((entry) => entry.id))
    const awaiting = (restorable.current ?? []).filter(
      (entry) => !liveIds.has(entry.id) && !retired.current.has(entry.id),
    )
    writeSession(TRACKED_KEY, JSON.stringify([...live, ...awaiting]))
  }, [jobs])

  return <JobsContext.Provider value={{ jobs, track, dismiss }}>{children}</JobsContext.Provider>
}
