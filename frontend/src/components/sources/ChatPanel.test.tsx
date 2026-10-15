import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { ChatPanel } from './ChatPanel'

// useTranslation is mocked globally in setup.ts (t returns the key string)

vi.mock('@/lib/hooks/use-modal-manager', () => ({
  useModalManager: () => ({ openModal: vi.fn() }),
}))

// Keep the message-content deps light for this composer-focused test.
vi.mock('@/components/sources/MessageActions', () => ({
  MessageActions: () => null,
}))

describe('ChatPanel composer', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    // jsdom does not implement scrollIntoView (used by the auto-scroll effect).
    window.HTMLElement.prototype.scrollIntoView = vi.fn()
  })

  const getTextarea = () => screen.getByRole('textbox') as HTMLTextAreaElement

  it('sends the typed message and clears the input on send-button click', () => {
    const onSendMessage = vi.fn()
    render(
      <ChatPanel
        messages={[]}
        isStreaming={false}
        contextIndicators={null}
        onSendMessage={onSendMessage}
      />
    )

    const textarea = getTextarea()
    fireEvent.change(textarea, { target: { value: '  hello world  ' } })

    const sendButton = screen.getByRole('button', { name: 'send' })
    fireEvent.click(sendButton)

    expect(onSendMessage).toHaveBeenCalledTimes(1)
    expect(onSendMessage).toHaveBeenCalledWith('hello world', undefined, undefined)
    expect(textarea.value).toBe('')
  })

  it('sends on Cmd+Enter on macOS', () => {
    const uaSpy = vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue(
      'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)'
    )
    const onSendMessage = vi.fn()
    render(
      <ChatPanel
        messages={[]}
        isStreaming={false}
        contextIndicators={null}
        onSendMessage={onSendMessage}
      />
    )

    const textarea = getTextarea()
    fireEvent.change(textarea, { target: { value: 'via cmd' } })
    fireEvent.keyDown(textarea, { key: 'Enter', metaKey: true, ctrlKey: false })

    expect(onSendMessage).toHaveBeenCalledWith('via cmd', undefined, undefined)
    expect(textarea.value).toBe('')
    uaSpy.mockRestore()
  })

  it('sends on Ctrl+Enter on non-macOS', () => {
    const uaSpy = vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue(
      'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
    )
    const onSendMessage = vi.fn()
    render(
      <ChatPanel
        messages={[]}
        isStreaming={false}
        contextIndicators={null}
        onSendMessage={onSendMessage}
      />
    )

    const textarea = getTextarea()
    fireEvent.change(textarea, { target: { value: 'via ctrl' } })
    fireEvent.keyDown(textarea, { key: 'Enter', ctrlKey: true, metaKey: false })

    expect(onSendMessage).toHaveBeenCalledWith('via ctrl', undefined, undefined)
    expect(textarea.value).toBe('')
    uaSpy.mockRestore()
  })

  it('does not send while streaming', () => {
    const onSendMessage = vi.fn()
    render(
      <ChatPanel
        messages={[]}
        isStreaming={true}
        contextIndicators={null}
        onSendMessage={onSendMessage}
      />
    )

    const textarea = getTextarea()
    // Textarea is disabled while streaming, but the guard must also hold.
    fireEvent.keyDown(textarea, { key: 'Enter', ctrlKey: true })

    expect(onSendMessage).not.toHaveBeenCalled()
  })
})

describe('ChatPanel history window', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    window.HTMLElement.prototype.scrollIntoView = vi.fn()
  })

  const baseProps = {
    messages: [],
    isStreaming: false,
    contextIndicators: null,
    onSendMessage: vi.fn(),
  }

  it('shows no counts when the conversation fits', () => {
    render(
      <ChatPanel
        {...baseProps}
        historySelection={{ kept_exchanges: 5, dropped_exchanges: 0, total_exchanges: 5, reason: 'budget' }}
      />
    )

    expect(screen.queryByText('chat.historyWindow')).not.toBeInTheDocument()
    // The range control itself always shows - it must be reachable
    // before trimming ever happens.
    expect(screen.getByRole('button', { name: '24h' })).toBeInTheDocument()
  })

  it('states what was sent when trimming applied', () => {
    render(
      <ChatPanel
        {...baseProps}
        historySelection={{ kept_exchanges: 32, dropped_exchanges: 368, total_exchanges: 400, reason: 'budget' }}
      />
    )

    // t() is mocked to return the key; interpolation is real.
    expect(screen.getByText('chat.historyWindow')).toBeInTheDocument()
  })

  it('sends the selected range as history_since', () => {
    const onSendMessage = vi.fn()
    render(<ChatPanel {...baseProps} onSendMessage={onSendMessage} />)

    fireEvent.click(screen.getByRole('button', { name: '24h' }))
    const textarea = screen.getByRole('textbox') as HTMLTextAreaElement
    fireEvent.change(textarea, { target: { value: 'q' } })
    fireEvent.click(screen.getByRole('button', { name: 'send' }))

    expect(onSendMessage).toHaveBeenCalledWith('q', undefined, '24h')
  })

  it('defaults to the whole history', () => {
    const onSendMessage = vi.fn()
    render(<ChatPanel {...baseProps} onSendMessage={onSendMessage} />)

    const textarea = screen.getByRole('textbox') as HTMLTextAreaElement
    fireEvent.change(textarea, { target: { value: 'q' } })
    fireEvent.click(screen.getByRole('button', { name: 'send' }))

    expect(onSendMessage).toHaveBeenCalledWith('q', undefined, undefined)
  })
})
