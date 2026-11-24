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

it('keeps multiple same-name chats checked across search and type filters', async () => {
  const fetchMock = vi.fn().mockResolvedValueOnce(Response.json({ chats }))
  vi.stubGlobal('fetch', fetchMock)
  render(<WhatsAppChatExporter />)

  expect(screen.getByRole('status').textContent).toBe('Loading chats…')
  expect(exportButton().disabled).toBe(true)
  const first = await screen.findByRole<HTMLInputElement>('checkbox', { name: /Alex.*Direct chat.*ID 41/ })
  const second = screen.getByRole<HTMLInputElement>('checkbox', { name: /Alex.*Direct chat.*ID 42/ })
  expect(screen.getByRole('checkbox', { name: /周末群聊.*Group chat/ })).toBeTruthy()

  fireEvent.click(first)
  fireEvent.click(second)
  expect(first.checked).toBe(true)
  expect(second.checked).toBe(true)
  expect(screen.getByText('2 chats selected')).toBeTruthy()
  expect(exportButton().disabled).toBe(false)

  fireEvent.change(screen.getByLabelText('Chat type'), { target: { value: 'direct' } })
  expect(screen.getAllByRole('checkbox')).toHaveLength(2)
  fireEvent.change(screen.getByLabelText('Search chats by name'), { target: { value: '  aLeX  ' } })
  expect(screen.getAllByRole('checkbox')).toHaveLength(2)
  fireEvent.change(screen.getByLabelText('Chat type'), { target: { value: 'group' } })
  expect(screen.queryAllByRole('checkbox')).toHaveLength(0)
  expect(screen.getByRole('status').textContent).toContain('No chats match')
  expect(screen.getByText('2 chats selected')).toBeTruthy()
  fireEvent.change(screen.getByLabelText('Search chats by name'), { target: { value: '周末' } })
  expect(screen.getAllByRole('checkbox')).toHaveLength(1)
  fireEvent.click(screen.getByRole('checkbox', { name: /周末群聊.*Group chat/ }))
  expect(screen.getByText('3 chats selected')).toBeTruthy()

  fireEvent.change(screen.getByLabelText('Search chats by name'), { target: { value: '' } })
  fireEvent.change(screen.getByLabelText('Chat type'), { target: { value: 'all' } })
  const restoredFirst = screen.getByRole<HTMLInputElement>('checkbox', { name: /Alex.*ID 41/ })
  expect(restoredFirst.checked).toBe(true)
  fireEvent.click(restoredFirst)
  expect(restoredFirst.checked).toBe(false)
  expect(screen.getByText('2 chats selected')).toBeTruthy()

  expect(fetchMock).toHaveBeenCalledTimes(1)
  expect(fetchMock.mock.calls[0][0]).toBe('/api/whatsapp/chats')
})

it('downloads one ZIP for all checked chats including selections hidden by filtering', async () => {
  let finishExport!: (response: Response) => void
  const exportResponse = new Promise<Response>((resolve) => { finishExport = resolve })
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(Response.json({ chats }))
    .mockReturnValueOnce(exportResponse)
  vi.stubGlobal('fetch', fetchMock)
  render(<WhatsAppChatExporter />)

  fireEvent.click(await screen.findByRole('checkbox', { name: /Alex.*ID 42/ }))
  fireEvent.change(screen.getByLabelText('Chat type'), { target: { value: 'group' } })
  const group = screen.getByRole<HTMLInputElement>('checkbox', { name: /周末群聊.*Group chat/ })
  fireEvent.click(group)
  expect(screen.queryByLabelText('File format')).toBeNull()
  fireEvent.click(exportButton())
  expect(exportButton('Exporting…').disabled).toBe(true)
  expect(group.disabled).toBe(true)
  expect(fetchMock.mock.calls[1]).toEqual(['/api/whatsapp/chats/export', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ chat_ids: [42, 99] }),
  }])

  const filename = '聊天记录.zip'
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
    .mockResolvedValueOnce(Response.json({ detail: 'Could not export these chats' }, { status: 503 }))
    .mockResolvedValueOnce(new Response(null, { headers: { 'Content-Type': 'application/zip' } }))
  vi.stubGlobal('fetch', fetchMock)
  render(<WhatsAppChatExporter />)

  expect((await screen.findByRole('alert')).textContent).toBe('Could not read WhatsApp chats')
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  fireEvent.click(await screen.findByRole('checkbox', { name: /Alex.*ID 41/ }))
  fireEvent.click(exportButton())
  expect((await screen.findByRole('alert')).textContent).toBe('Could not export these chats')
  expect(exportButton().disabled).toBe(false)
  expect(saveBlob).not.toHaveBeenCalled()

  fireEvent.click(exportButton())
  expect((await screen.findByRole('status')).textContent).toContain('WhatsApp_Chats.zip')
  expect(saveBlob).toHaveBeenCalledTimes(1)
})
