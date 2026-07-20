import { describe, it, expect, vi } from 'vitest'
import { api, artifactUrl } from './api'

describe('api helpers', () => {
  it('builds artifact URLs under /api', () => {
    expect(artifactUrl('abc123')).toBe('/api/artifacts/abc123')
  })

  it('sends a JSON body for POSTs', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ job_id: 'j1' }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    const out = await api.purgeDelete('/tmp/cache', ['/tmp/cache/a.log'])
    expect(out).toEqual({ job_id: 'j1' })

    const [url, opts] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/purge/delete')
    expect(opts.method).toBe('POST')
    expect(opts.headers['Content-Type']).toBe('application/json')
    expect(JSON.parse(opts.body)).toEqual({
      folder: '/tmp/cache',
      files: ['/tmp/cache/a.log'],
    })
    vi.unstubAllGlobals()
  })

  it('passes FormData through without a JSON content-type', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ job_id: 'j2' }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    const form = new FormData()
    form.append('files', new Blob(['x']), 'a.docx')
    await api.docToPdf(form)

    const [, opts] = fetchMock.mock.calls[0]
    expect(opts.body).toBe(form) // browser sets the multipart boundary
    expect(opts.headers['Content-Type']).toBeUndefined()
    vi.unstubAllGlobals()
  })

  it('surfaces the detail message from an error response', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: '❌ nope' }), {
        status: 400,
        headers: { 'content-type': 'application/json' },
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    await expect(api.remuxScan('/x')).rejects.toThrow('❌ nope')
    vi.unstubAllGlobals()
  })

  it('falls back to status text when the error body is not JSON', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response('boom', { status: 500, statusText: 'Server Error' }))
    vi.stubGlobal('fetch', fetchMock)
    await expect(api.health()).rejects.toThrow(/500/)
    vi.unstubAllGlobals()
  })
})
