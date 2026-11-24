import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, it, expect, vi } from 'vitest'
import { ApiError } from '../api'
import { ToolActiveContext } from '../toolHost'
import TorrentDownloader from './TorrentDownloader'

const apiMock = vi.hoisted(() => ({
  torrentStatus: vi.fn(),
  torrentDevices: vi.fn(),
  torrentResolveMagnet: vi.fn(),
  torrentPollResolve: vi.fn(),
  torrentDiscard: vi.fn(),
  torrentDeviceSelect: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api')>()),
  api: apiMock,
}))

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.resetAllMocks()
})

describe('magnet resolution', () => {
  const INFOHASH = 'aaaabbbbccccddddeeeeffff0000111122223333'

  // Drain pending work without advancing the metadata polling interval.
  const flush = () => act(async () => void (await vi.advanceTimersByTimeAsync(0)))

  beforeEach(() => {
    vi.useFakeTimers()
    apiMock.torrentStatus.mockResolvedValue({
      running: true,
      server: 'BitComet',
      detail: null,
      url: 'http://127.0.0.1:19377',
    })
    apiMock.torrentDevices.mockResolvedValue({ active: 'local', devices: [] })
    apiMock.torrentResolveMagnet.mockResolvedValue({
      infohash: INFOHASH,
      ready: false,
      name: null,
      files: [],
      state: 'awaiting_metadata',
    })
    // Discard deletes the BitComet task, so a poll crossing it gets a 404.
    apiMock.torrentPollResolve.mockRejectedValue(new ApiError('Unknown torrent.', 404))
    apiMock.torrentDiscard.mockResolvedValue({ infohash: INFOHASH, state: 'discarded' })
  })

  it('names the reason a magnet was refused', async () => {
    apiMock.torrentResolveMagnet.mockRejectedValue(new ApiError('Not a magnet link.', 400))
    render(<TorrentDownloader />)
    await flush()

    fireEvent.change(screen.getByLabelText('Magnet links'), { target: { value: 'nonsense' } })
    fireEvent.click(screen.getByRole('button', { name: 'Resolve' }))
    await flush()
    expect(screen.getByText(/nonsense — Not a magnet link\./)).toBeTruthy()
  })

  it('stops counting a discarded magnet as fetching without reporting a failure', async () => {
    render(<TorrentDownloader />)
    await flush()

    fireEvent.change(screen.getByLabelText('Magnet links'), {
      target: { value: `magnet:?xt=urn:btih:${INFOHASH}` },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Resolve' }))
    await flush()
    expect(screen.queryAllByText(/fetching metadata for/)).toHaveLength(1)

    fireEvent.click(screen.getByRole('button', { name: 'Discard' }))
    await flush()
    expect(apiMock.torrentDiscard).toHaveBeenCalledWith(INFOHASH)
    expect(screen.queryAllByText(/fetching metadata for/)).toHaveLength(0)

    // Let the poll that was already sleeping answer.
    await act(async () => void (await vi.advanceTimersByTimeAsync(5000)))
    expect(screen.queryAllByText(/Failed/)).toHaveLength(0)
  })
})

describe('device selection', () => {
  const NAS = { id: 'nas', label: 'Basement NAS', url: '192.168.1.50:19377', username: 'me', has_password: true, is_local: false }
  const MAC = { id: 'local', label: 'This Mac', url: null, username: '', has_password: false, is_local: true }
  const BOOK = { active: 'nas', devices: [NAS, MAC] }
  const NAS_UP = { running: true, server: 'BitComet', detail: null, url: 'http://192.168.1.50:19377', device: NAS, is_local: false, save_folders: ['/vol1'] }
  const NAS_DOWN = { running: false, server: null, detail: 'Basement NAS is not answering.', url: 'http://192.168.1.50:19377', device: NAS, is_local: false, save_folders: [] }
  const MAC_UP = { running: true, server: 'BitComet', detail: null, url: 'http://127.0.0.1:19377', device: MAC, is_local: true, save_folders: [] }

  const page = (active: boolean) => (
    <ToolActiveContext.Provider value={active}>
      <TorrentDownloader />
    </ToolActiveContext.Provider>
  )
  const flush = () => act(async () => void (await Promise.resolve()))

  it('keeps the picked device instead of letting the stale answer win', async () => {
    let landSlow: (s: unknown) => void = () => {}
    apiMock.torrentStatus
      .mockResolvedValueOnce(NAS_UP)
      .mockImplementationOnce(() => new Promise((r) => (landSlow = r)))
      .mockResolvedValue(MAC_UP)
    apiMock.torrentDevices.mockResolvedValue(BOOK)
    apiMock.torrentDeviceSelect.mockResolvedValue({ ...BOOK, active: 'local' })

    const { rerender } = render(page(true))
    await flush()

    // Leaving and returning to the tab re-probes; the sleeping NAS takes seconds.
    rerender(page(false))
    rerender(page(true))
    await flush()

    fireEvent.click(screen.getByRole('button', { name: 'Change device' }))
    fireEvent.click(screen.getAllByRole('radio')[1]!)
    await flush()
    await flush()
    expect(apiMock.torrentDeviceSelect).toHaveBeenCalledWith('local')

    await act(async () => {
      landSlow(NAS_DOWN)
      await Promise.resolve()
    })
    expect(screen.queryAllByText('Basement NAS is not answering.')).toHaveLength(0)
    expect(screen.queryAllByText('http://192.168.1.50:19377')).toHaveLength(0)
  })

  it('says why a switch did not take, with no device form open', async () => {
    apiMock.torrentStatus.mockResolvedValue(NAS_UP)
    apiMock.torrentDevices.mockResolvedValue(BOOK)
    apiMock.torrentDeviceSelect.mockRejectedValue(new ApiError('No such device.', 404))

    render(page(true))
    await flush()

    fireEvent.click(screen.getByRole('button', { name: 'Change device' }))
    fireEvent.click(screen.getAllByRole('radio')[1]!)
    await flush()
    await flush()
    expect(screen.getByText('No such device.')).toBeTruthy()
  })
})
