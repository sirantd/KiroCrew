/**
 * The welcome screen's memory chip sits directly above the composer, and only
 * while the welcome state shows (empty non-orchestrator session). WelcomeView is
 * mocked to nothing here, so any chip found comes from ChatPage's own slot.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, act, fireEvent, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import chatReducer, { setActiveSlot } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'

interface VirtuosoMockProps {
  data?: unknown[]
  itemContent: (index: number, item: unknown) => ReactNode
}
vi.mock('react-virtuoso', () => ({ Virtuoso: ({ data, itemContent }: VirtuosoMockProps) => <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div> }))

type Msg = { role: string; content: string }
const detail = vi.hoisted(() => ({ messages: [] as Msg[] }))
const createChatSlot = vi.hoisted(() => vi.fn())
const deleteChatSlot = vi.hoisted(() => vi.fn().mockResolvedValue(undefined))
vi.mock('../api/client', () => ({
  api: {
    createChatSlot,
    deleteChatSlot,
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn(async () => ({ messages: detail.messages, running: false, has_more: false, total: detail.messages.length })),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    slackChannels: vi.fn().mockResolvedValue([]),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPage from '../pages/ChatPage'
// The surface registry is populated by module side effect, and only `App.tsx`
// imports it in production -- so a harness that mounts ChatPage directly starts
// with an EMPTY registry and every surface lookup misses. Import it here for the
// same reason the app does. (The miss degrades safely to the surface-free
// sentence, which is what the unregistered-surface case below asserts.)
import '../surfaces/builtins'

type Slot = { messages: Msg[]; mode?: string; slotKeys?: string[] }

function makeStore({ messages, mode = '', slotKeys = ['slot-a'] }: Slot) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null,
        slots: slotKeys.map(key => ({ key, messages: key === 'slot-a' ? messages.length : 0, running: false, mode: key === 'slot-a' ? mode : '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined })),
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot: 'slot-a', messages,
        slotRunning: false, slotStopping: false, slotState: 'idle',
        history: [], historyHasMore: false, pendingInput: null,
        unresumableResume: null, lastResumeRequestId: null,
        subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools',
        slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
        slotStatusDetail: {}, slotContextPct: {}, slotActivity: {}, slotHistory: [],
        historyOffset: 0, _wsChunkedDuringFetch: false,
        slotMessages: {}, slotLoading: false,
      } as unknown as RootState['chat'],
      notifications: { items: [] } as unknown as RootState['notifications'],
    },
  })
}

async function renderWith(slot: Slot) {
  detail.messages = slot.messages
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = makeStore(slot)
  await act(async () => {
    render(
      <QueryClientProvider client={qc}>
        <Provider store={store}>
          <ThemeProvider>
            <MemoryRouter><ChatPage /></MemoryRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>,
    )
  })
  return store
}

describe('memory chip above the composer', () => {
  it('renders above the composer on the welcome state', async () => {
    await renderWith({ messages: [] })
    const chip = screen.getByTestId('composer-memory-chip')
    expect(chip.textContent).toContain('Choose memory mode')
    const composer = screen.getAllByRole('textbox').at(-1)!
    // DOCUMENT_POSITION_FOLLOWING: the composer comes after the chip.
    expect(chip.compareDocumentPosition(composer) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('shows an error when creating the replacement slot fails', async () => {
    createChatSlot.mockRejectedValueOnce(new Error('Memory mode switch failed'))
    await renderWith({ messages: [] })

    fireEvent.click(screen.getByText('Choose memory mode').closest('button')!)
    fireEvent.click(screen.getByText('Incognito').closest('button')!)

    await waitFor(() => expect(screen.getByTestId('memory-mode-error')).toHaveTextContent('Memory mode switch failed'))
  })

  it('preserves the composer draft when switching memory mode', async () => {
    createChatSlot.mockResolvedValueOnce({ key: 'slot-b', messages: 0, running: false, memory_mode: 'incognito' })
    sessionStorage.setItem('mc-chat-file-drafts', JSON.stringify({ 'slot-a': ['/scratch/brief.txt'] }))
    const store = await renderWith({ messages: [] })
    const composer = screen.getAllByRole('textbox').at(-1)! as HTMLTextAreaElement

    fireEvent.change(composer, { target: { value: 'keep this draft' } })
    fireEvent.click(screen.getByText('Choose memory mode').closest('button')!)
    fireEvent.click(screen.getByText('Incognito').closest('button')!)

    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('slot-b'))
    expect(composer.value).toBe('keep this draft')
    expect(JSON.parse(localStorage.getItem('mc-chat-drafts') || '{}')).toEqual({ 'slot-b': 'keep this draft' })
    expect(JSON.parse(sessionStorage.getItem('mc-chat-file-drafts') || '{}')).toEqual({ 'slot-b': ['/scratch/brief.txt'] })
  })

  it('abandons a pending memory-mode switch after the user changes slots', async () => {
    let resolveCreate!: (slot: { key: string; messages: number; running: boolean; memory_mode: string }) => void
    const pendingCreate = new Promise<{ key: string; messages: number; running: boolean; memory_mode: string }>(resolve => {
      resolveCreate = resolve
    })
    createChatSlot.mockReturnValueOnce(pendingCreate)
    localStorage.setItem('mc-chat-drafts', JSON.stringify({ 'slot-a': 'draft for A', 'slot-other': 'draft for B' }))
    const store = await renderWith({ messages: [], slotKeys: ['slot-a', 'slot-other'] })
    const composer = screen.getAllByRole('textbox').at(-1)! as HTMLTextAreaElement
    expect(composer.value).toBe('draft for A')

    fireEvent.click(screen.getByText('Choose memory mode').closest('button')!)
    fireEvent.click(screen.getByText('Incognito').closest('button')!)
    await waitFor(() => expect(createChatSlot).toHaveBeenCalled())

    act(() => { store.dispatch(setActiveSlot('slot-other')) })
    await waitFor(() => expect(composer.value).toBe('draft for B'))

    await act(async () => {
      resolveCreate({ key: 'slot-replacement', messages: 0, running: false, memory_mode: 'incognito' })
      await pendingCreate
    })

    await waitFor(() => expect(deleteChatSlot).toHaveBeenCalledWith('slot-replacement'))
    expect(store.getState().chat.activeSlot).toBe('slot-other')
    expect(composer.value).toBe('draft for B')
    expect(JSON.parse(localStorage.getItem('mc-chat-drafts') || '{}')).toEqual({
      'slot-a': 'draft for A',
      'slot-other': 'draft for B',
    })
  })

  it('is absent once the session has messages', async () => {
    await renderWith({ messages: [{ role: 'user', content: 'hello' }, { role: 'assistant', content: 'hi' }] })
    expect(screen.queryByTestId('composer-memory-chip')).toBeNull()
  })

  it('is absent in orchestrator mode, which keeps the chip in its own view', async () => {
    await renderWith({ messages: [], mode: 'orchestrator' })
    expect(screen.queryByTestId('composer-memory-chip')).toBeNull()
  })
})
