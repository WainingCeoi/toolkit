import { useState } from 'react'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import Layout from './Layout'
import { JobsContext, type JobsContextValue } from './jobs'
import { useToolBusy } from './toolHost'

vi.mock('./pages', () => ({
  PAGES: {
    'web-images-to-pdf': function CapturePage() {
      const [url, setUrl] = useState('')
      const [busy, setBusy] = useState(false)
      useToolBusy(busy)
      return (
        <div>
          <label>Page URL<input value={url} onChange={e => setUrl(e.target.value)} /></label>
          <button onClick={() => setBusy(!busy)}>{busy ? 'Finish capture' : 'Begin capture'}</button>
        </div>
      )
    },
    'cache-purge': function CleanupPage() { return <h1>Cleanup test</h1> },
  },
  isToolSlug: (slug: string) => ['web-images-to-pdf', 'cache-purge'].includes(slug),
}))

const manifest = [{ name: 'Files & Tools', tools: [
  { slug: 'web-images-to-pdf', title: '🌐 Web Images to PDF', description: 'Capture images from a web page.' },
  { slug: 'cache-purge', title: '🧹 Cache Purge', description: 'Find and remove junk files.' },
] }]

beforeEach(() => {
  vi.stubGlobal('localStorage', { getItem: () => null, setItem: vi.fn() })
  sessionStorage.clear()
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })))
  Element.prototype.scrollIntoView = vi.fn()
  vi.stubGlobal('fetch', vi.fn((url: string) => Promise.resolve(new Response(JSON.stringify(
    url.endsWith('/health') ? { ok: true, ffmpeg: true, soffice: true, mineru: true } : manifest,
  ), { headers: { 'Content-Type': 'application/json' } }))))
})

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

function mount(path = '/tools/web-images-to-pdf', overrides: Partial<JobsContextValue> = {}) {
  const value: JobsContextValue = { jobs: {}, track: async () => null, dismiss: vi.fn(), ...overrides }
  const router = createMemoryRouter([{ path: '*', element: <Layout /> }], { initialEntries: [path] })
  render(<JobsContext.Provider value={value}><RouterProvider router={router} /></JobsContext.Provider>)
  return { router, value }
}

describe('workspace navigation', () => {
  it('preserves tool input when switching tabs and chooses a neighbour after close', async () => {
    const { router } = mount()
    await screen.findByRole('link', { name: 'Cache Purge' })
    const url = 'https://example.test/page'
    fireEvent.change(screen.getByLabelText('Page URL'), { target: { value: url } })
    fireEvent.click(screen.getByRole('link', { name: 'Cache Purge' }))
    await screen.findByRole('heading', { name: 'Cleanup test' })
    fireEvent.click(screen.getByRole('navigation', { name: 'Open tools' }).querySelector('a')!)
    expect((screen.getByLabelText('Page URL') as HTMLInputElement).value).toBe(url)
    fireEvent.click(screen.getByRole('button', { name: 'Close Web Images to PDF' }))
    await waitFor(() => expect(router.state.location.pathname).toBe('/tools/cache-purge'))
    expect(screen.queryByLabelText('Page URL')).toBeNull()
  })

  it('holds a tab open while a capture is busy', async () => {
    mount()
    fireEvent.click(await screen.findByRole('button', { name: 'Begin capture' }))
    expect((screen.getByRole('button', { name: 'Close Web Images to PDF' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: 'Finish capture' }))
    expect((screen.getByRole('button', { name: 'Close Web Images to PDF' }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('searches descriptions and opens the result from the keyboard', async () => {
    const { router } = mount('/')
    const navigation = within(screen.getByRole('navigation', { name: 'Tool navigation' }))
    await navigation.findByRole('link', { name: 'Web Images to PDF' })
    const search = screen.getByLabelText('Find a tool')
    fireEvent.change(search, { target: { value: 'junk' } })
    expect(navigation.queryByRole('link', { name: 'Web Images to PDF' })).toBeNull()
    fireEvent.keyDown(search, { key: 'Enter' })
    await waitFor(() => expect(router.state.location.pathname).toBe('/tools/cache-purge'))
  })

  it('exposes running jobs in the header and blocks closing their tool', async () => {
    mount('/', { jobs: { job1: { toolPath: '/tools/cache-purge', snapshot: {
      id: 'job1', tool: 'cache-purge', state: 'running', message: 'Working', items: [],
      result: null, error: null, created_at: '',
    } } } })
    expect(await screen.findByRole('link', { name: '1 running' })).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Close Cache Purge' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('shows an empty workspace after a successful manifest with all tools disabled', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response('[]', {
      headers: { 'Content-Type': 'application/json' },
    }))))
    mount('/')
    expect(await screen.findByRole('heading', { name: 'No tools enabled' })).toBeTruthy()
    expect(screen.queryByLabelText('Loading tools')).toBeNull()
    expect(screen.queryByRole('link', { name: 'Cache Purge' })).toBeNull()
  })

  it('opens the mobile navigation with slash and returns focus on Escape', async () => {
    vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() })))
    mount('/')
    const navigation = screen.getByRole('navigation', { name: 'Tool navigation' })
    expect(navigation.inert).toBe(true)
    fireEvent.keyDown(window, { key: '/' })
    expect(navigation.inert).toBe(false)
    expect(document.activeElement).toBe(screen.getByLabelText('Find a tool'))
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(navigation.inert).toBe(true)
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Open navigation' }))
  })
})
