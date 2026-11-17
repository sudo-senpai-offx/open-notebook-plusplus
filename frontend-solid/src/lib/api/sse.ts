// Minimal SSE reader for the source-chat streaming endpoint. Consumes the
// `data:`-framed stream incrementally and hands each decoded payload to the
// callback. Returns when the stream ends.

export interface SseMessage {
  event?: string
  data: string
}

export async function streamSse(
  url: string,
  body: Record<string, unknown>,
  onMessage: (msg: SseMessage) => void,
): Promise<void> {
  const token = localStorage.getItem('onb_token')
  const response = await fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify(body),
  })

  if (!response.ok) {
    throw new Error(`SSE request failed: ${response.status}`)
  }

  const reader = response.body!.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })
      const frames = buffer.split('\n\n')
      // Keep the last (possibly incomplete) frame in the buffer.
      buffer = frames.pop() ?? ''

      for (const frame of frames) {
        if (!frame.trim()) continue
        let event: string | undefined
        const dataLines: string[] = []
        for (const line of frame.split('\n')) {
          if (line.startsWith('event:')) {
            event = line.slice(6).trim()
          } else if (line.startsWith('data:')) {
            dataLines.push(line.slice(5).trim())
          }
        }
        if (dataLines.length > 0) {
          onMessage({ event, data: dataLines.join('\n') })
        }
      }
    }
  } finally {
    reader.releaseLock()
  }
}