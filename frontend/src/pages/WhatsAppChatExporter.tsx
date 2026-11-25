import { useEffect, useState } from 'react'
import { api, saveBlob } from '../api'
import Button from '../components/Button'
import EmptyState from '../components/EmptyState'
import Icon from '../components/Icon'
import ToolHeader from '../components/ToolHeader'
import { useToolActive, useToolBusy } from '../toolHost'
import type { WhatsAppChat } from '../types/api'

type ChatFilter = 'all' | WhatsAppChat['type']

type ChatLoad =
  | { status: 'loading' }
  | { status: 'ready'; chats: WhatsAppChat[] }
  | { status: 'error'; message: string }

type Download =
  | { status: 'idle' | 'working' }
  | { status: 'saved'; filename: string }
  | { status: 'error'; message: string }

const CHAT_TYPE_LABEL = { direct: 'Direct chat', group: 'Group chat' }

function chatDetails(chat: WhatsAppChat, nameCounts: Map<string, number>) {
  const type = CHAT_TYPE_LABEL[chat.type]
  return (nameCounts.get(chat.name) ?? 0) > 1 ? `${type} · ID ${chat.id}` : type
}

export default function WhatsAppChatExporter() {
  const [chatLoad, setChatLoad] = useState<ChatLoad>({ status: 'loading' })
  const [search, setSearch] = useState('')
  const [filter, setFilter] = useState<ChatFilter>('all')
  const [selectedChatIds, setSelectedChatIds] = useState(() => new Set<number>())
  const [download, setDownload] = useState<Download>({ status: 'idle' })
  const active = useToolActive()
  const exporting = download.status === 'working'
  useToolBusy(exporting)

  useEffect(() => {
    if (!active || chatLoad.status !== 'loading') return
    let cancelled = false
    api.whatsappChats().then(
      ({ chats }) => {
        if (!cancelled) setChatLoad({ status: 'ready', chats })
      },
      (error: Error) => {
        if (!cancelled) setChatLoad({ status: 'error', message: error.message })
      },
    )
    return () => { cancelled = true }
  }, [active, chatLoad.status])

  const chats = chatLoad.status === 'ready' ? chatLoad.chats : []
  const selectedCount = selectedChatIds.size
  const nameCounts = new Map<string, number>()
  for (const { name } of chats) nameCounts.set(name, (nameCounts.get(name) ?? 0) + 1)

  const query = search.trim().toLowerCase()
  const matches = chats.filter((chat) =>
    (filter === 'all' || chat.type === filter) && chat.name.toLowerCase().includes(query),
  )

  function toggleChat(chatId: number, checked: boolean) {
    setSelectedChatIds((previous) => {
      const next = new Set(previous)
      if (checked) next.add(chatId)
      else next.delete(chatId)
      return next
    })
    setDownload({ status: 'idle' })
  }

  async function exportChats() {
    if (selectedCount === 0 || exporting) return
    setDownload({ status: 'working' })
    try {
      const { blob, filename } = await api.whatsappExport([...selectedChatIds])
      saveBlob(blob, filename)
      setDownload({ status: 'saved', filename })
    } catch (error) {
      setDownload({ status: 'error', message: (error as Error).message })
    }
  }

  return (
    <div>
      <ToolHeader slug="whatsapp" />
      <p className="page-sub">
        Download messages, replies, and reactions from WhatsApp chats synced to the Toolkit Mac.
      </p>

      <div className="station">
        <section className="panel" aria-labelledby="whatsapp-chats">
          <h2 className="step" id="whatsapp-chats"><span>Chats</span></h2>
          {chatLoad.status === 'loading' && <div className="note info" role="status">Loading chats…</div>}
          {chatLoad.status === 'error' && (
            <>
              <div className="note error" role="alert">{chatLoad.message}</div>
              <Button onClick={() => setChatLoad({ status: 'loading' })}>Retry</Button>
            </>
          )}
          {chatLoad.status === 'ready' && chats.length === 0 && (
            <>
              <EmptyState icon="search" title="No chats found">
                Open WhatsApp on the Toolkit Mac and let it sync your chats.
              </EmptyState>
              <Button onClick={() => setChatLoad({ status: 'loading' })}>Reload chats</Button>
            </>
          )}
          {chats.length > 0 && (
            <>
              <div className="row">
                <div className="field grow">
                  <label htmlFor="whatsapp-search">Search chats by name</label>
                  <input
                    id="whatsapp-search"
                    className="control"
                    type="search"
                    value={search}
                    onChange={(event) => setSearch(event.target.value)}
                  />
                </div>
                <div className="field">
                  <label htmlFor="whatsapp-filter">Chat type</label>
                  <select
                    id="whatsapp-filter"
                    className="control"
                    value={filter}
                    onChange={(event) => setFilter(event.target.value as ChatFilter)}
                  >
                    <option value="all">All chats</option>
                    <option value="direct">Direct chats</option>
                    <option value="group">Group chats</option>
                  </select>
                </div>
              </div>
              {matches.length > 0 ? (
                <div className="whatsapp-chat-list" role="group" aria-label="Choose chats">
                  {matches.map((chat) => (
                    <label className="whatsapp-chat" key={chat.id}>
                      <input
                        type="checkbox"
                        checked={selectedChatIds.has(chat.id)}
                        onChange={(event) => toggleChat(chat.id, event.target.checked)}
                        disabled={exporting}
                      />
                      <span className="grow">
                        <span className="whatsapp-chat-name">{chat.name}</span>
                        <span className="whatsapp-chat-meta">{chatDetails(chat, nameCounts)}</span>
                      </span>
                    </label>
                  ))}
                </div>
              ) : (
                <div className="note info" role="status">No chats match your search and filters.</div>
              )}
            </>
          )}
        </section>

        <section className="panel result-panel" aria-labelledby="whatsapp-download">
          <h2 className="step" id="whatsapp-download"><span>Export</span></h2>
          <div className="action-summary">
            <strong aria-live="polite">
              {selectedCount === 0
                ? 'Select chats to export'
                : `${selectedCount} ${selectedCount === 1 ? 'chat' : 'chats'} selected`}
            </strong>
            <p>Each chat includes one JSONL file plus locally available attachments and stickers.</p>
          </div>
          <Button variant="primary" onClick={exportChats} disabled={selectedCount === 0} loading={exporting}>
            {!exporting && <Icon name="download" size={16} />}
            {exporting ? 'Exporting…' : 'Export ZIP'}
          </Button>
          {download.status === 'error' && <div className="note error" role="alert">{download.message}</div>}
          {download.status === 'saved' && (
            <div className="note ok" role="status">Download started for <strong>{download.filename}</strong>.</div>
          )}
        </section>
      </div>
    </div>
  )
}
