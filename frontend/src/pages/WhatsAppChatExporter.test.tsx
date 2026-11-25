import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { saveBlob } from '../api'
import WhatsAppChatExporter from './WhatsAppChatExporter'

vi.mock('../api', async (importOriginal) => ({
  ...await importOriginal<typeof import('../api')>(),
  saveBlob: vi.fn(),
}))

const chats = [
  { id: 41, name: 'Alex', type: 'direct' },
  { id: 42, name: 'Alex', type: 'direct' },
  { id: 99, name: '周末群聊', type: 'group' },
]

function exportButton(name = 'Export ZIP') {
  return screen.getByRole('button', { name }) as HTMLButtonElement
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  vi.unstubAllGlobals()
})

it('replaces the selected chat even when filters hide it', async () => {
  const fetchMock = vi.fn().mockResolvedValueOnce(Response.json({ chats }))
  vi.stubGlobal('fetch', fetchMock)
  render(<WhatsAppChatExporter />)

  expect(exportButton().disabled).toBe(true)
  const first = await screen.findByRole<HTMLInputElement>('radio', { name: /Alex.*Direct chat.*ID 41/ })
  const second = screen.getByRole<HTMLInputElement>('radio', { name: /Alex.*Direct chat.*ID 42/ })
  fireEvent.click(first)
  fireEvent.click(second)
  expect(first.checked).toBe(false)
  expect(second.checked).toBe(true)

  fireEvent.change(screen.getByLabelText('Chat type'), { target: { value: 'group' } })
  fireEvent.change(screen.getByLabelText('Search chats by name'), { target: { value: '周末' } })
  expect(screen.queryByRole('radio', { name: /Alex.*ID 42/ })).toBeNull()
  expect(screen.getByText('Alex · Direct chat · ID 42')).toBeTruthy()
  expect(exportButton().disabled).toBe(false)
  const group = screen.getByRole('radio', { name: /周末群聊.*Group chat/ })
  fireEvent.click(group)

  fireEvent.change(screen.getByLabelText('Search chats by name'), { target: { value: '' } })
  fireEvent.change(screen.getByLabelText('Chat type'), { target: { value: 'all' } })
  expect(screen.getAllByRole('radio', { checked: true })).toEqual([group])
  expect(fetchMock).toHaveBeenCalledTimes(1)
})

it('requests only the selected chat and downloads its ZIP with the Unicode filename', async () => {
  let finishExport!: (response: Response) => void
  const exportResponse = new Promise<Response>((resolve) => { finishExport = resolve })
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(Response.json({ chats }))
    .mockReturnValueOnce(exportResponse)
  vi.stubGlobal('fetch', fetchMock)
  render(<WhatsAppChatExporter />)

  fireEvent.click(await screen.findByRole('radio', { name: /Alex.*ID 42/ }))
  const group = screen.getByRole<HTMLInputElement>('radio', { name: /周末群聊.*Group chat/ })
  fireEvent.click(group)
  fireEvent.click(exportButton())
  expect(exportButton('Exporting…').disabled).toBe(true)
  expect(group.disabled).toBe(true)
  expect(fetchMock.mock.calls[1]).toEqual(['/api/whatsapp/chats/export', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ chat_ids: [99] }),
  }])

  const filename = 'WhatsApp_周末群聊.zip'
  const body = new Uint8Array(22)
  body.set([0x50, 0x4b, 0x05, 0x06]) // End of central directory for an empty ZIP.
  await act(async () => finishExport(new Response(body, {
    headers: {
      'Content-Type': 'application/zip',
      'Content-Disposition': `attachment; filename*=UTF-8''${encodeURIComponent(filename)}`,
    },
  })))

  expect(screen.getByRole('status').textContent).toContain(filename)
  expect(vi.mocked(saveBlob)).toHaveBeenCalledTimes(1)
  const [blob, downloadedName] = vi.mocked(saveBlob).mock.calls[0]
  expect(downloadedName).toBe(filename)
  expect(blob.type).toBe('application/zip')
  expect(new Uint8Array(await blob.arrayBuffer())).toEqual(body)
})

it('surfaces list and export errors and lets the user retry', async () => {
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(Response.json({ detail: 'Could not read WhatsApp chats' }, { status: 503 }))
    .mockResolvedValueOnce(Response.json({ chats }))
    .mockResolvedValueOnce(Response.json({ detail: 'Could not export this chat' }, { status: 503 }))
    .mockResolvedValueOnce(new Response(null, { headers: { 'Content-Type': 'application/zip' } }))
  vi.stubGlobal('fetch', fetchMock)
  render(<WhatsAppChatExporter />)

  expect((await screen.findByRole('alert')).textContent).toBe('Could not read WhatsApp chats')
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  fireEvent.click(await screen.findByRole('radio', { name: /Alex.*ID 41/ }))
  fireEvent.click(exportButton())
  expect((await screen.findByRole('alert')).textContent).toBe('Could not export this chat')
  expect(exportButton().disabled).toBe(false)
  expect(saveBlob).not.toHaveBeenCalled()

  fireEvent.click(exportButton())
  expect((await screen.findByRole('status')).textContent).toContain('WhatsApp.zip')
  expect(saveBlob).toHaveBeenCalledTimes(1)
})
