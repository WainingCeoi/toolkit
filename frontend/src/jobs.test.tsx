import { act, renderHook, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { ReactNode } from 'react'
import { JobsContext, useToolJob, type AnyJob, type JobsContextValue } from './jobs'

const TOOL = '/tools/cache-purge'

// track() never resolves: the job has started but no frame has arrived.
function harness(overrides: Partial<JobsContextValue> = {}) {
  const value: JobsContextValue = {
    jobs: {},
    track: vi.fn((): Promise<AnyJob | null> => new Promise(() => {})),
    dismiss: vi.fn(),
    ...overrides,
  }
  const wrapper = ({ children }: { children: ReactNode }) => (
    <JobsContext.Provider value={value}>{children}</JobsContext.Provider>
  )
  return { value, wrapper }
}

describe('useToolJob', () => {
  it('reports running from the click, not from the first progress frame', async () => {
    const { wrapper } = harness()
    let release: (value: { job_id: string }) => void = () => {}
    const startFn = vi.fn(() => new Promise<{ job_id: string }>((r) => (release = r)))

    const { result } = renderHook(() => useToolJob(TOOL), { wrapper })
    expect(result.current.running).toBe(false)

    act(() => void result.current.start(startFn))
    await waitFor(() => expect(result.current.running).toBe(true))

    await act(async () => release({ job_id: 'j1' }))
    expect(result.current.running).toBe(true)
  })

  it('releases the gate when the start request fails', async () => {
    const { wrapper } = harness()
    const { result } = renderHook(() => useToolJob(TOOL), { wrapper })

    await act(async () => {
      await result.current.start(() => Promise.reject(new Error('nope')))
    })

    expect(result.current.running).toBe(false)
    expect(result.current.error).toBe('nope')
  })

  it('picks the newest job by creation time, not by map order', () => {
    const snap = (id: string, state: 'running' | 'done', created: string) => ({
      id,
      tool: 'remux',
      state,
      message: '',
      items: [],
      result: null,
      error: null,
      created_at: created,
    })
    const { wrapper } = harness({
      jobs: {
        // Newer-but-running first, older-but-done second: map order lies.
        b: { toolPath: TOOL, snapshot: snap('b', 'running', '2026-08-10T12:00:00Z') },
        a: { toolPath: TOOL, snapshot: snap('a', 'done', '2026-08-10T11:00:00Z') },
      },
    })
    const { result } = renderHook(() => useToolJob(TOOL), { wrapper })
    expect(result.current.snapshot?.id).toBe('b')
    expect(result.current.running).toBe(true)
  })

  it('still reports running for a job it did not start itself', () => {
    const { wrapper } = harness({
      jobs: {
        j9: {
          toolPath: TOOL,
          snapshot: {
            id: 'j9',
            tool: 'cache-purge',
            state: 'running',
            message: '',
            items: [],
            result: null,
            error: null,
            created_at: '',
          },
        },
      },
    })
    const { result } = renderHook(() => useToolJob(TOOL), { wrapper })
    expect(result.current.running).toBe(true)
  })
})
