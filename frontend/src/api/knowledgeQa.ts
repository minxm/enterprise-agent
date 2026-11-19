import type { ApiResponse } from '../types/api';
import { conversationListParams, type ConversationSummary } from '../knowledge/sessions';
import { createSseParser, type SseFrame } from './sse';
import { RequestError, createRequestId, currentAuthorization, request } from './request';

export const CONVERSATIONS_PATH = '/api/v1/agent/conversations';
export const KNOWLEDGE_QA_STREAM_PATH = '/api/v1/agent/knowledge-qa/stream';
export const CONVERSATION_PAGE_SIZE = 20;

export interface ConversationPage {
  items: ConversationSummary[];
  page: number;
  size: number;
  total: number;
}

export interface ConversationCreated {
  conversationId: string;
}

export interface HistorySource {
  chunkId?: string;
  chunk_id?: string;
  content?: string;
}

export interface HistoryMessage {
  role: string;
  content: string;
  status: string;
  sourceIds?: string[];
  sources?: HistorySource[];
}

export function conversationMessagesPath(conversationId: string): string {
  return `${CONVERSATIONS_PATH}/${conversationId}/messages`;
}

export async function fetchConversations(page = 0, size = CONVERSATION_PAGE_SIZE): Promise<ConversationPage> {
  const response = await request.get<ApiResponse<ConversationPage>>(CONVERSATIONS_PATH, {
    params: conversationListParams(page, size),
  });
  const data = response.data.data;
  return {
    items: data?.items ?? [],
    page: data?.page ?? page,
    size: data?.size ?? size,
    total: data?.total ?? 0,
  };
}

export async function fetchConversationMessages(conversationId: string): Promise<HistoryMessage[]> {
  const response = await request.get<ApiResponse<{ messages?: HistoryMessage[] }>>(conversationMessagesPath(conversationId));
  return response.data.data?.messages ?? [];
}

export async function createConversation(): Promise<string> {
  const response = await request.post<ApiResponse<ConversationCreated>>(CONVERSATIONS_PATH);
  const conversationId = response.data.data?.conversationId;
  if (!conversationId) {
    throw new RequestError(response.data.message || '没有返回会话', response.data.code);
  }
  return conversationId;
}

export async function streamKnowledgeQa(
  input: { conversationId: string; question: string; topK?: number; retrievalStrategy?: string },
  onFrame: (frame: SseFrame) => void,
  signal: AbortSignal,
): Promise<void> {
  const headers = new Headers({
    Accept: 'text/event-stream',
    'Content-Type': 'application/json',
    'X-Request-ID': createRequestId(),
  });
  const authorization = currentAuthorization();
  if (authorization) {
    headers.set('Authorization', authorization);
  }
  const response = await fetch(resolveUrl(KNOWLEDGE_QA_STREAM_PATH), {
    method: 'POST',
    headers,
    body: JSON.stringify({
      conversationId: input.conversationId,
      question: input.question,
      topK: input.topK,
      retrievalStrategy: input.retrievalStrategy,
    }),
    signal,
  });
  await readSseResponse(response, onFrame, signal);
}

export async function readSseResponse(response: Response, onFrame: (frame: SseFrame) => void, signal?: AbortSignal): Promise<void> {
  if (!response.ok) {
    throw await httpError(response);
  }
  if (!response.body) {
    throw new RequestError('响应没有事件流', response.status);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8');
  const parser = createSseParser(onFrame);
  try {
    while (true) {
      const { done, value } = await Promise.race([reader.read(), abortAsError(signal)]);
      if (done) {
        parser.push(decoder.decode());
        return;
      }
      parser.push(decoder.decode(value, { stream: true }));
    }
  } finally {
    await reader.cancel().catch(() => undefined);
  }
}

function abortAsError(signal?: AbortSignal): Promise<never> {
  if (!signal) {
    return new Promise(() => undefined);
  }
  if (signal.aborted) {
    return Promise.reject(new DOMException('The operation was aborted.', 'AbortError'));
  }
  return new Promise((_, reject) => {
    signal.addEventListener(
      'abort',
      () => reject(new DOMException('The operation was aborted.', 'AbortError')),
      { once: true },
    );
  });
}

async function httpError(response: Response): Promise<RequestError> {
  const text = await response.text();
  let message = `请求失败 (${response.status})`;
  try {
    const body = JSON.parse(text) as { message?: unknown };
    if (typeof body.message === 'string' && body.message.length > 0) {
      message = body.message;
    }
  } catch {
    if (text.trim()) {
      message = '事件数据无法解析';
    }
  }
  return new RequestError(message, response.status);
}

function resolveUrl(path: string): string {
  const base = import.meta.env.VITE_API_BASE_URL ?? '';
  return `${base}${path}`;
}
