// Global job tracking: any tool can start a job here and its live snapshot
// stays available app-wide — the bottom dock renders every tracked job, so
// work keeps visibly running while you switch tools (the thing the old
// Streamlit app structurally could not do).

import React, { createContext, useCallback, useContext, useRef, useState } from 'react'
import { followJob } from './api'

const JobsContext = createContext(null)

export function JobsProvider({ children }) {
  const [jobs, setJobs] = useState({}) // id -> {snapshot, toolPath}
  const followed = useRef(new Set())

  const track = useCallback((jobId, toolPath) => {
    if (followed.current.has(jobId)) return Promise.resolve(null)
    followed.current.add(jobId)
    const update = (snapshot) =>
      setJobs((prev) => ({ ...prev, [jobId]: { snapshot, toolPath } }))
    return followJob(jobId, update).catch((err) => {
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

  const snapshot = jobId ? jobs[jobId]?.snapshot : null
  const running = snapshot ? snapshot.state === 'running' : false
  return { start, snapshot, running, error, setError }
}
