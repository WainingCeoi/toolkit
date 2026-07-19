import { describe, it, expect, beforeEach, vi } from 'vitest'
import { api, artifactUrl, getAuthToken, setAuthToken } from './api'

describe('api helpers', () => {
  beforeEach(() => {
    localStorage.clear()
    document.cookie = 'toolkit_auth=; path=/; max-age=0'
  })

  it('builds artifact URLs under /api', () => {
    expect(artifactUrl('abc123')).toBe('/api/artifacts/abc123')
  })

  it('round-trips the auth token through localStorage and cookie', () => {
    setAuthToken('sekret')
    expect(getAuthToken()).toBe('sekret')
    expect(document.cookie).toContain('toolkit_auth=sekret')
  })

  it('sends a JSON body and attaches the auth header when set', async () => {
    setAuthToken('tok')
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
    expect(JSON.parse(opts.body)).toEqual({
      folder: '/tmp/cache',
      files: ['/tmp/cache/a.log'],
    })
    expect(opts.headers.Authorization).toBe('Bearer tok')
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

  it('dispatches an auth-required event on 401', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response('', { status: 401 }))
    vi.stubGlobal('fetch', fetchMock)
    const onAuth = vi.fn()
    window.addEventListener('toolkit-auth-required', onAuth)
    await expect(api.health()).rejects.toThrow(/Authentication required/)
    expect(onAuth).toHaveBeenCalled()
    window.removeEventListener('toolkit-auth-required', onAuth)
    vi.unstubAllGlobals()
  })
})
