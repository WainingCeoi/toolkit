import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { JobsContext, type JobsContextValue } from '../jobs'
import MagnetScraper from './MagnetScraper'

const savedConfig = {
  website_url: 'https://videos.example/catalog',
  cutoff_video: 'https://videos.example/watch/old',
  website_url_set: true,
  cutoff_set: true,
}

const emptyConfig = {
  website_url: '',
  cutoff_video: '',
  website_url_set: false,
  cutoff_set: false,
}

function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function deferredResponse() {
  let resolve!: (value: Response) => void
  const promise = new Promise<Response>((complete) => { resolve = complete })
  return { promise, resolve }
}

function context(jobs: JobsContextValue['jobs'] = {}): JobsContextValue {
  return { jobs, track: vi.fn(async () => null), dismiss: vi.fn() }
}

function completedContext() {
  return context({
    completed: {
      toolPath: '/tools/magnet-scraper',
      snapshot: {
        id: 'completed',
        tool: 'magnet-scraper',
        state: 'done',
        created_at: '2026-10-16T12:00:00Z',
        message: '',
        items: [],
        error: null,
        result: { cutoff_found: true, urls: [], successful: [], failed: [] },
      },
    },
  })
}

function page(value = context()) {
  return <JobsContext.Provider value={value}><MagnetScraper /></JobsContext.Provider>
}

function sourceInput() {
  return screen.getByLabelText('Source website') as HTMLInputElement
}

function cutoffInput() {
  return screen.getByLabelText('Last watched video URL') as HTMLInputElement
}

function autoButton() {
  return screen.getByRole('button', { name: /Find new magnet links/ }) as HTMLButtonElement
}

function openSettings() {
  fireEvent.click(screen.getByRole('button', { name: 'Settings' }))
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

it('waits for configuration before enabling automatic discovery', async () => {
  const load = deferredResponse()
  vi.stubGlobal('fetch', vi.fn().mockReturnValue(load.promise))
  render(page())

  expect(autoButton().disabled).toBe(true)
  await act(async () => load.resolve(response(savedConfig)))
  await waitFor(() => expect(autoButton().disabled).toBe(false))
})

it('saves settings through the API and makes an unconfigured source ready to run', async () => {
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(response(emptyConfig))
    .mockResolvedValueOnce(response(savedConfig))
  vi.stubGlobal('fetch', fetchMock)
  render(page())

  openSettings()
  await screen.findByLabelText('Source website')
  expect(autoButton().disabled).toBe(true)
  fireEvent.change(sourceInput(), { target: { value: savedConfig.website_url } })
  fireEvent.change(cutoffInput(), { target: { value: savedConfig.cutoff_video } })
  fireEvent.click(screen.getByRole('button', { name: 'Save settings' }))

  expect((await screen.findByRole('status')).textContent).toBe(
    'Settings saved. Changes apply to your next automatic run.',
  )
  expect(screen.getByRole('form', { name: 'Magnet settings' })).toBeTruthy()
  expect(autoButton().disabled).toBe(false)
  expect(sourceInput().value).toBe(savedConfig.website_url)
  expect(cutoffInput().value).toBe(savedConfig.cutoff_video)
  const [url, options] = fetchMock.mock.calls[1]
  expect(url).toBe('/api/magnet/config')
  expect(options.method).toBe('PUT')
  expect(options.headers['Content-Type']).toBe('application/json')
  expect(JSON.parse(options.body)).toEqual({
    website_url: savedConfig.website_url,
    cutoff_video: savedConfig.cutoff_video,
  })
})

it('prefills saved settings and discards cancelled changes', async () => {
  const fetchMock = vi.fn().mockResolvedValue(response(savedConfig))
  vi.stubGlobal('fetch', fetchMock)
  render(page())
  await waitFor(() => expect(autoButton().disabled).toBe(false))

  openSettings()
  expect(sourceInput().value).toBe(savedConfig.website_url)
  expect(cutoffInput().value).toBe(savedConfig.cutoff_video)
  fireEvent.change(sourceInput(), { target: { value: 'https://draft.example/' } })
  fireEvent.change(cutoffInput(), { target: { value: 'https://draft.example/watch/1' } })
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))

  expect(screen.queryByRole('form', { name: 'Magnet settings' })).toBeNull()
  openSettings()
  expect(sourceInput().value).toBe(savedConfig.website_url)
  expect(cutoffInput().value).toBe(savedConfig.cutoff_video)
  expect(fetchMock).toHaveBeenCalledTimes(1)
})

it('allows a failed settings load to be retried while other modes remain usable', async () => {
  const fetchMock = vi.fn()
    .mockRejectedValueOnce(new TypeError('Connection unavailable'))
    .mockResolvedValueOnce(response(savedConfig))
  vi.stubGlobal('fetch', fetchMock)
  render(page())

  expect((await screen.findByRole('alert')).textContent).toContain('Connection unavailable')
  expect(autoButton().disabled).toBe(true)
  fireEvent.click(screen.getByRole('button', { name: 'Manual' }))
  expect((screen.getByRole('button', { name: 'Collect magnet links' }) as HTMLButtonElement).disabled).toBe(false)
  fireEvent.click(screen.getByRole('button', { name: 'Deduplicate' }))
  expect((screen.getByRole('button', { name: 'Remove duplicates' }) as HTMLButtonElement).disabled).toBe(false)
  fireEvent.click(screen.getByRole('button', { name: 'Automatic' }))
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))

  await waitFor(() => expect(autoButton().disabled).toBe(false))
  expect(screen.queryByRole('alert')).toBeNull()
  expect(fetchMock).toHaveBeenCalledTimes(2)
})

it('keeps edits after a failed save and prevents concurrent saves or automatic runs', async () => {
  const save = deferredResponse()
  const updated = { ...savedConfig, cutoff_video: 'https://videos.example/watch/new' }
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(response(savedConfig))
    .mockReturnValueOnce(save.promise)
    .mockResolvedValueOnce(response(updated))
  vi.stubGlobal('fetch', fetchMock)
  render(page())
  await waitFor(() => expect(autoButton().disabled).toBe(false))
  openSettings()
  fireEvent.change(cutoffInput(), { target: { value: updated.cutoff_video } })
  fireEvent.click(screen.getByRole('button', { name: 'Save settings' }))

  expect(sourceInput().disabled).toBe(true)
  expect(cutoffInput().disabled).toBe(true)
  expect((screen.getByRole('button', { name: 'Save settings' }) as HTMLButtonElement).disabled).toBe(true)
  expect((screen.getByRole('button', { name: 'Cancel' }) as HTMLButtonElement).disabled).toBe(true)
  expect(autoButton().disabled).toBe(true)
  fireEvent.click(screen.getByRole('button', { name: 'Save settings' }))
  expect(fetchMock).toHaveBeenCalledTimes(2)

  await act(async () => save.resolve(response({ detail: 'Could not save settings' }, 500)))
  expect((await screen.findByRole('alert')).textContent).toContain('Could not save settings')
  expect(sourceInput().disabled).toBe(false)
  expect(cutoffInput().value).toBe(updated.cutoff_video)
  fireEvent.click(screen.getByRole('button', { name: 'Save settings' }))

  await screen.findByRole('status')
  expect(screen.queryByRole('alert')).toBeNull()
  expect(fetchMock).toHaveBeenCalledTimes(3)
  expect(cutoffInput().value).toBe(updated.cutoff_video)
  expect(autoButton().disabled).toBe(false)
})

it('refreshes an advanced cutoff after a run without replacing unsaved settings', async () => {
  const advancedConfig = { ...savedConfig, cutoff_video: 'https://videos.example/watch/latest' }
  const refresh = deferredResponse()
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(response(savedConfig))
    .mockReturnValueOnce(refresh.promise)
  vi.stubGlobal('fetch', fetchMock)
  const view = render(page())
  await waitFor(() => expect(autoButton().disabled).toBe(false))
  openSettings()
  fireEvent.change(cutoffInput(), { target: { value: 'https://videos.example/watch/draft' } })

  view.rerender(page(completedContext()))

  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
  await act(async () => refresh.resolve(response(advancedConfig)))
  expect(cutoffInput().value).toBe('https://videos.example/watch/draft')
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
  openSettings()
  expect(cutoffInput().value).toBe(advancedConfig.cutoff_video)
})

it('keeps saved settings when an overlapping refresh returns older values', async () => {
  const save = deferredResponse()
  const refresh = deferredResponse()
  const updated = {
    ...savedConfig,
    website_url: 'https://new.example/catalog',
    cutoff_video: 'https://new.example/watch/latest',
  }
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(response(savedConfig))
    .mockReturnValueOnce(save.promise)
    .mockReturnValueOnce(refresh.promise)
  vi.stubGlobal('fetch', fetchMock)
  const view = render(page())
  await waitFor(() => expect(autoButton().disabled).toBe(false))
  openSettings()
  fireEvent.change(sourceInput(), { target: { value: updated.website_url } })
  fireEvent.change(cutoffInput(), { target: { value: updated.cutoff_video } })
  fireEvent.click(screen.getByRole('button', { name: 'Save settings' }))

  view.rerender(page(completedContext()))
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3))
  await act(async () => save.resolve(response(updated)))
  await act(async () => refresh.resolve(response(savedConfig)))

  expect(sourceInput().value).toBe(updated.website_url)
  expect(cutoffInput().value).toBe(updated.cutoff_video)
  expect(screen.getByText('Settings saved. Changes apply to your next automatic run.').getAttribute('role')).toBe('status')
  expect(autoButton().disabled).toBe(false)
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
  openSettings()
  expect(sourceInput().value).toBe(updated.website_url)
  expect(cutoffInput().value).toBe(updated.cutoff_video)
})
