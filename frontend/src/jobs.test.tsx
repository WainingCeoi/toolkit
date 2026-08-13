// useToolJob's gate on starting a second copy of the same work.
//
// This is the layer where the double-submit regressions lived, and until now
// nothing here was testable: the job core is hooks and context, so the pure
// helper tests next door could not reach it.

import { act, renderHook, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { ReactNode } from 'react'
import { JobsContext, useToolJob, type AnyJob, type JobsContextValue } from './jobs'

const TOOL = '/tools/cache-purge'

/** A jobs context whose track() never resolves, standing in for a job that has
 *  started on the server but whose first progress frame has not arrived. */
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
    // The window this covers is the whole start request. For a batch of masks
    // or a multi-file upload that is seconds, and every page gates its Start
    // button on `running` alone — so while this was false the button stayed
    // live and a second click launched a duplicate of the same heavy job.
    const { wrapper } = harness()
    let release: (value: { job_id: string }) => void = () => {}
    const startFn = vi.fn(() => new Promise<{ job_id: string }>((r) => (release = r)))

    const { result } = renderHook(() => useToolJob(TOOL), { wrapper })
    expect(result.current.running).toBe(false)

    act(() => void result.current.start(startFn))
    await waitFor(() => expect(result.current.running).toBe(true))

    // Still running after the POST resolves: the snapshot that would carry the
    // gate does not exist until the stream delivers its first frame.
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

  it('still reports running for a job it did not start itself', () => {
    // Reload and revisit both land here: the page has no local job id, so the
    // gate has to come from whatever the provider is tracking for this tool.
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
