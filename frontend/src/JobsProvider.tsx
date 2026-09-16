// Component-only module: Fast Refresh needs it to export nothing but a component.

import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { ApiError, api, followJob } from './api'
import { JobsContext, type AnyJob, type TrackedJob } from './jobs'
import { readSessionArray, writeSession } from './sessionStore'

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

// An evicted-job SSE frame carries only { state }; fill the fields the dock reads.
function normalizeSnapshot(snapshot: Partial<AnyJob>): AnyJob {
  return { items: [], message: '', result: null, error: null, ...snapshot } as AnyJob
}

export function JobsProvider({ children }: { children: ReactNode }) {
  const [jobs, setJobs] = useState<Record<string, TrackedJob>>({})
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

  // Probe before tracking: following an evicted job would raise a spurious "failed" chip.
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
        .catch((err: unknown) => {
          // Only a 404 retires the id; any other failure leaves it for the next reload.
          if (err instanceof ApiError && err.status === 404) retired.current.add(id)
        })
    }
    return () => {
      cancelled = true
    }
  }, [track])

  // Keep untracked, unretired ids: the first commit runs before the restore pass lands.
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
