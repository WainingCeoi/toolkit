// Global job tracking: any tool can start a job here and its live snapshot
// stays available app-wide — the bottom dock renders every tracked job, so
// work keeps visibly running while you switch tools (the thing the old
// Streamlit app structurally could not do).

import React, { createContext, useCallback, useContext, useMemo, useRef, useState } from 'react'
import { followJob } from './api'

const JobsContext = createContext(null)

// Defensive shape: an evicted-job SSE frame carries only { state }, so fill the
// fields the dock and panels read (items/message/result/error) before storing.
function normalizeSnapshot(snapshot) {
  return { items: [], message: '', result: null, error: null, ...snapshot }
}

export function JobsProvider({ children }) {
  const [jobs, setJobs] = useState({}) // id -> {snapshot, toolPath}
  const followed = useRef(new Set())

  const track = useCallback((jobId, toolPath) => {
    if (followed.current.has(jobId)) return Promise.resolve(null)
    followed.current.add(jobId)
    const update = (snapshot) =>
      setJobs((prev) => ({ ...prev, [jobId]: { snapshot: normalizeSnapshot(snapshot), toolPath } }))
    return followJob(jobId, update).catch((err) => {
      // A genuine failure (not a transient blip — followJob polls through
      // those): mark it failed and drop it from `followed` so it can be
      // re-tracked later.
      followed.current.delete(jobId)
      setJobs((prev) => {
        const cur = prev[jobId]
        if (!cur) return prev
        const snap = { ...cur.snapshot, state: 'failed', error: err.message }
        return { ...prev, [jobId]: { ...cur, snapshot: snap } }
      })
      throw err
    })
  }, [])

  const dismiss = useCallback((jobId) => {
    followed.current.delete(jobId)
    setJobs((prev) => {
      const next = { ...prev }
      delete next[jobId]
      return next
    })
  }, [])

  return (
    <JobsContext.Provider value={{ jobs, track, dismiss }}>
      {children}
    </JobsContext.Provider>
  )
}

export function useJobs() {
  return useContext(JobsContext)
}

// Convenience for tool pages: start + track + expose the live snapshot of
// the page's own most recent job.
export function useToolJob(toolPath) {
  const { jobs, track } = useJobs()
  const [jobId, setJobId] = useState(null)
  const [error, setError] = useState(null)

  const start = useCallback(
    async (startFn) => {
      setError(null)
      try {
        const { job_id: id } = await startFn()
        setJobId(id)
        track(id, toolPath).catch((err) => setError(err.message))
        return id
      } catch (err) {
        setError(err.message)
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

  const snapshot = activeId ? jobs[activeId]?.snapshot : null
  const running = snapshot ? snapshot.state === 'running' : false
  return { start, snapshot, running, error, setError }
}
