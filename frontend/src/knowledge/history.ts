import { emptyRun, type Citation, type RunView } from './events';

export interface StoredHistorySource {
  chunkId?: string;
  chunk_id?: string;
  content?: string;
}

export interface StoredHistoryMessage {
  role: string;
  content: string;
  status: string;
  sourceIds?: string[];
  sources?: StoredHistorySource[];
}

export interface HistoryTurn {
  id: string;
  question: string;
  run: RunView;
}

const CONVERSATION_KEY = 'enterprise-agent.conversation-id';
const UUID_TEXT = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export function rememberConversationId(conversationId: string | undefined): void {
  try {
    if (!conversationId) {
      globalThis.sessionStorage?.removeItem(CONVERSATION_KEY);
      return;
    }
    globalThis.sessionStorage?.setItem(CONVERSATION_KEY, conversationId);
  } catch {
    // 浏览器拒绝写入时仍使用当前内存中的会话编号。
  }
}

export function restoreConversationId(): string | undefined {
  try {
    const value = globalThis.sessionStorage?.getItem(CONVERSATION_KEY);
    return value && UUID_TEXT.test(value) ? value : undefined;
  } catch {
    return undefined;
  }
}

export function turnsFromHistory(messages: StoredHistoryMessage[]): HistoryTurn[] {
  const turns: HistoryTurn[] = [];
  for (let index = 0; index < messages.length; index += 1) {
    const message = messages[index];
    if (message.role !== 'user') {
      continue;
    }
    const next = messages[index + 1];
    const assistant = next?.role === 'assistant' ? next : undefined;
    const status = assistant?.status ?? message.status;
    const failed = status === 'failed';
    turns.push({
      id: `history-${index}`,
      question: message.content,
      run: {
        ...emptyRun(),
        completed: !failed,
        answer: assistant?.content ?? '',
        sources: citations(assistant),
        error: failed ? assistant?.content || '本轮失败' : null,
      },
    });
    if (assistant) {
      index += 1;
    }
  }
  return turns;
}

/** 只有明确的 404 表示当前用户不能使用这个会话编号。 */
export function historyFailureClearsConversation(status: number | undefined): boolean {
  return status === 404;
}

function citations(message: StoredHistoryMessage | undefined): Citation[] {
  return (message?.sources ?? []).flatMap((source) => {
    const content = source.content?.trim() ?? '';
    if (!content) {
      return [];
    }
    return [{ documentId: '', chunkId: source.chunkId || source.chunk_id || '', content }];
  });
}
