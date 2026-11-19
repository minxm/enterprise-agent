export interface SseFrame {
  event: string;
  data: string;
}

export interface SseParser {
  push(text: string): void;
}

/** Incremental SSE parser. A frame is emitted only after a blank line. */
export function createSseParser(onFrame: (frame: SseFrame) => void, onComment?: (comment: string) => void): SseParser {
  let buffer = '';

  return {
    push(text: string) {
      buffer += text.replace(/\r\n/g, '\n').replace(/\r/g, '\n');
      let splitAt = buffer.indexOf('\n\n');
      while (splitAt >= 0) {
        const raw = buffer.slice(0, splitAt);
        buffer = buffer.slice(splitAt + 2);
        emitFrame(raw, onFrame, onComment);
        splitAt = buffer.indexOf('\n\n');
      }
    },
  };
}

export function decodeUtf8(chunks: Uint8Array[]): string {
  const decoder = new TextDecoder('utf-8');
  let text = '';
  chunks.forEach((chunk, index) => {
    text += decoder.decode(chunk, { stream: index < chunks.length - 1 });
  });
  text += decoder.decode();
  return text;
}

function emitFrame(raw: string, onFrame: (frame: SseFrame) => void, onComment?: (comment: string) => void): void {
  if (raw.trim() === '') {
    return;
  }
  let eventName = 'message';
  const data: string[] = [];
  let comment = '';
  let sawField = false;
  for (const line of raw.split('\n')) {
    if (line.startsWith(':')) {
      comment += line.slice(1).trim();
      continue;
    }
    const separator = line.indexOf(':');
    const field = separator === -1 ? line : line.slice(0, separator);
    let value = separator === -1 ? '' : line.slice(separator + 1);
    if (value.startsWith(' ')) {
      value = value.slice(1);
    }
    if (field === 'event') {
      eventName = value;
      sawField = true;
    } else if (field === 'data') {
      data.push(value);
      sawField = true;
    }
  }
  if (comment && !sawField) {
    onComment?.(comment);
    return;
  }
  if (!sawField) {
    return;
  }
  onFrame({ event: eventName, data: data.join('\n') });
}
