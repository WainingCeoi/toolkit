import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { JobsContext } from '../jobs'
import CachePurge from './CachePurge'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

it.each([503, 409])('handles a rejected delete with status %s', async (status) => {
  const response = (body: unknown, code = 200) => new Response(JSON.stringify(body), {
    status: code,
    headers: { 'Content-Type': 'application/json' },
  })
  vi.stubGlobal('fetch', vi.fn()
    .mockResolvedValueOnce(response({
      scan_id: 'scan1', files: ['/tmp/a.log'], errors: [], total_bytes: 4, rejected_tokens: [],
    }))
    .mockResolvedValueOnce(response({ detail: 'Cannot start this job' }, status)))
  render(
    <JobsContext.Provider value={{ jobs: {}, track: async () => null, dismiss: () => {} }}>
      <CachePurge />
    </JobsContext.Provider>,
  )
  fireEvent.click(screen.getByRole('button', { name: /Scan folder/ }))
  await screen.findByText('a.log')
  fireEvent.click(screen.getByRole('checkbox'))
  fireEvent.click(screen.getByRole('button', { name: /Delete 1 file/ }))
  await screen.findByText('Cannot start this job')

  const retry = screen.queryByRole('button', { name: /Delete 1 file/ })
  if (status === 503) {
    expect(retry).not.toBeNull()
    expect((retry as HTMLButtonElement).disabled).toBe(false)
  } else {
    expect(retry).toBeNull()
  }
})
