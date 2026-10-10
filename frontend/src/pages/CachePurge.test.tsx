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

it('invalidates the delete preview when patterns change', async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
    scan_id: 'scan1', files: ['/tmp/a.log'], errors: [], total_bytes: 4, rejected_tokens: [],
  }), { headers: { 'Content-Type': 'application/json' } }))
  vi.stubGlobal('fetch', fetchMock)
  render(
    <JobsContext.Provider value={{ jobs: {}, track: async () => null, dismiss: () => {} }}>
      <CachePurge />
    </JobsContext.Provider>,
  )
  fireEvent.click(screen.getByRole('button', { name: /Scan folder/ }))
  await screen.findByText('a.log')
  fireEvent.click(screen.getByRole('checkbox'))

  const patterns = screen.getByLabelText('Cache extensions / patterns') as HTMLInputElement
  const original = patterns.value
  fireEvent.change(patterns, { target: { value: '*.tmp' } })
  expect(screen.queryByRole('button', { name: /Delete 1 file/ })).toBeNull()
  expect(screen.queryByText('a.log')).toBeNull()
  expect(screen.getByText('Preview needs a refresh')).toBeTruthy()

  fireEvent.change(patterns, { target: { value: original } })
  expect((screen.getByRole('button', { name: /Delete 1 file/ }) as HTMLButtonElement).disabled).toBe(true)
  expect(fetchMock).toHaveBeenCalledTimes(1)
})
