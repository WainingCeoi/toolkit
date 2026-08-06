import { describe, it, expect, vi } from 'vitest'
import { api, artifactUrl, watermarkImageUrl, watermarkMaskUrl } from './api'

describe('api helpers', () => {
  it('builds artifact URLs under /api', () => {
    expect(artifactUrl('abc123')).toBe('/api/artifacts/abc123')
  })

  it('posts a magnet as form data, not JSON', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ infohash: 'abc', ready: false, files: [] }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    await api.torrentResolveMagnet('magnet:?xt=urn:btih:abc', '~/Movies')

    const [url, opts] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/torrent/resolve')
    // The endpoint accepts a magnet OR an upload, so it is multipart on both
    // paths; sending JSON here would 422.
    expect(opts.body).toBeInstanceOf(FormData)
    expect((opts.body as FormData).get('magnet')).toBe('magnet:?xt=urn:btih:abc')
    expect((opts.body as FormData).get('save_dir')).toBe('~/Movies')
    vi.unstubAllGlobals()
  })

  it('sends the selection as JSON on send', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ infohash: 'abc', task_id: '1001', name: 'X' }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    await api.torrentSend({ infohash: 'abc', selected: [1, 3] })

    const [url, opts] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/torrent')
    // No save_dir: BitComet fixes a task's folder when the task is created, so
    // the destination travels with /resolve instead.
    expect(JSON.parse(opts.body)).toEqual({ infohash: 'abc', selected: [1, 3] })
    vi.unstubAllGlobals()
  })

  it('uploads a watermark batch as multipart and runs it as JSON', async () => {
    // A fresh Response per call — a body can only be read once.
    const fetchMock = vi.fn().mockImplementation(() =>
      Promise.resolve(
        new Response(JSON.stringify({ job_id: 'j9' }), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        }),
      ),
    )
    vi.stubGlobal('fetch', fetchMock)

    const fd = new FormData()
    fd.append('files', new Blob(['x']), 'a.png')
    await api.watermarkUpload(fd)
    await api.watermarkRun({ batch_id: 'b1', inpainter: 'cv2', masks: { i1: 'AA==' } })

    const [uploadUrl, uploadOpts] = fetchMock.mock.calls[0]
    expect(uploadUrl).toBe('/api/watermark/batch')
    expect(uploadOpts.body).toBe(fd)

    const [runUrl, runOpts] = fetchMock.mock.calls[1]
    expect(runUrl).toBe('/api/watermark/run')
    // The masks are base64 strings inside JSON — NOT another multipart form.
    expect(JSON.parse(runOpts.body)).toEqual({
      batch_id: 'b1',
      inpainter: 'cv2',
      masks: { i1: 'AA==' },
    })
    vi.unstubAllGlobals()
  })

  it('builds watermark image and mask URLs under /api', () => {
    expect(watermarkImageUrl('b1', 'i1')).toBe('/api/watermark/b1/i1/image')
    // The detector rides in the URL so changing it refetches the proposal,
    // the same way the sensitivity slider does.
    expect(watermarkMaskUrl('b1', 'i1', 70)).toBe(
      '/api/watermark/b1/i1/mask?sensitivity=70&detector=pattern',
    )
    expect(watermarkMaskUrl('b1', 'i1', 40, 'texture')).toBe(
      '/api/watermark/b1/i1/mask?sensitivity=40&detector=texture',
    )
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
