import { describe, expect, it } from 'vitest';
import { createSseParser, decodeUtf8, type SseFrame } from './sse';

function collect(chunks: string[]): SseFrame[] {
  const frames: SseFrame[] = [];
  const parser = createSseParser((frame) => frames.push(frame));
  for (const chunk of chunks) {
    parser.push(chunk);
  }
  return frames;
}

describe('sse parser', () => {
  it('keeps an event that arrives in several chunks', () => {
    const frames = collect(['event: run_star', 'ted\ndata: {"status":"started"}\n', '\n']);
    expect(frames).toEqual([{ event: 'run_started', data: '{"status":"started"}' }]);
  });

  it('reads several events from one chunk and accepts CRLF', () => {
    const frames = collect([
      'event: node_started\r\ndata: {"node":"knowledge_qa"}\r\n\r\nevent: node_completed\r\ndata: {"node":"knowledge_qa"}\r\n\r\n',
    ]);
    expect(frames.map((frame) => frame.event)).toEqual(['node_started', 'node_completed']);
  });

  it('joins a Chinese payload split inside a UTF-8 character', () => {
    const encoded = new TextEncoder().encode('event: message\ndata: {"answer":"中文答案"}\n\n');
    const split = encoded.findIndex((_, index) => encoded[index] === 0xe4);
    const text = decodeUtf8([encoded.slice(0, split + 1), encoded.slice(split + 1)]);
    const frames = collect([text]);
    expect(frames[0]?.data).toContain('中文答案');
  });

  it('ignores heartbeat comments', () => {
    const comments: string[] = [];
    const frames: SseFrame[] = [];
    const parser = createSseParser(
      (frame) => frames.push(frame),
      (comment) => comments.push(comment),
    );
    parser.push(': heartbeat\n\n');
    parser.push('event: run_completed\ndata: {"status":"answered"}\n\n');
    expect(comments).toEqual(['heartbeat']);
    expect(frames).toEqual([{ event: 'run_completed', data: '{"status":"answered"}' }]);
  });
});
