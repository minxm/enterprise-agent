import { describe, expect, it } from 'vitest';
import { KNOWLEDGE_QA_STREAM_PATH, CONVERSATIONS_PATH, conversationMessagesPath, readSseResponse } from './knowledgeQa';
import { conversationListParams } from '../knowledge/sessions';
import { RequestError, setAccessTokenReader } from './request';
import { streamKnowledgeQa } from './knowledgeQa';

describe('knowledge qa client', () => {
  it('calls only the Java routes', () => {
    expect(CONVERSATIONS_PATH).toBe('/api/v1/agent/conversations');
    expect(KNOWLEDGE_QA_STREAM_PATH).toBe('/api/v1/agent/knowledge-qa/stream');
    expect(KNOWLEDGE_QA_STREAM_PATH).not.toContain('8000');
    expect(KNOWLEDGE_QA_STREAM_PATH).not.toContain('/internal/');
    const historyPath = conversationMessagesPath('00000000-0000-4000-8000-00000000c291');
    expect(historyPath).toBe('/api/v1/agent/conversations/00000000-0000-4000-8000-00000000c291/messages');
    expect(historyPath).not.toContain(':8000');
    expect(historyPath).not.toContain('/internal/');
    expect(historyPath).not.toContain('thread_id');
    expect(conversationListParams(0, 20)).toEqual({ page: 0, size: 20 });
    expect(JSON.stringify(conversationListParams(0, 20))).not.toContain('tenant');
    expect(JSON.stringify(conversationListParams(0, 20))).not.toContain('thread');
  });

  it('rejects a non-2xx response without treating it as events', async () => {
    const response = new Response(JSON.stringify({ message: '未认证' }), { status: 401 });
    await expect(readSseResponse(response, () => undefined)).rejects.toMatchObject({
      message: '未认证',
      status: 401,
    });
  });

  it('reports invalid JSON from an error response', async () => {
    const response = new Response('not-json', { status: 502 });
    await expect(readSseResponse(response, () => undefined)).rejects.toBeInstanceOf(RequestError);
    await expect(readSseResponse(new Response('not-json', { status: 502 }), () => undefined)).rejects.toMatchObject({
      message: '事件数据无法解析',
    });
  });

  it('cancels the reader when the caller aborts', async () => {
    const controller = new AbortController();
    let cancelled = false;
    const response = new Response(
      new ReadableStream({
        pull() {
          return new Promise(() => undefined);
        },
        cancel() {
          cancelled = true;
        },
      }),
    );
    const pending = readSseResponse(response, () => undefined, controller.signal);
    controller.abort();
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' });
    expect(cancelled).toBe(true);
  });

  it('sends the in-memory access token to Java', async () => {
    setAccessTokenReader(() => 'memory-token');
    const calls: RequestInit[] = [];
    const original = globalThis.fetch;
    globalThis.fetch = (async (_url: string, init?: RequestInit) => {
      calls.push(init ?? {});
      return new Response(': heartbeat\n\n', { status: 200 });
    }) as typeof fetch;
    try {
      await streamKnowledgeQa({ conversationId: 'c1', question: '问题' }, () => undefined, new AbortController().signal);
    } finally {
      globalThis.fetch = original;
      setAccessTokenReader(() => undefined);
    }
    const headers = new Headers(calls[0]?.headers);
    expect(headers.get('Authorization')).toBe('Bearer memory-token');
    expect(String(calls[0]?.body)).not.toContain('threadId');
    expect(String(calls[0]?.body)).not.toContain('thread_id');
  });
});
