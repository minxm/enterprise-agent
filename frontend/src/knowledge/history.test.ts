import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  historyFailureClearsConversation,
  rememberConversationId,
  restoreConversationId,
  turnsFromHistory,
} from './history';

const CONVERSATION = '00000000-0000-4000-8000-00000000c291';

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('conversation id session', () => {
  it('restores only the conversation id and ignores a stored message cache', () => {
    const storage = memoryStorage();
    storage.setItem('enterprise-agent.knowledge-session', JSON.stringify({
      turns: [{ question: '旧缓存', answer: '不应恢复' }],
    }));
    vi.stubGlobal('sessionStorage', storage);

    rememberConversationId(CONVERSATION);

    expect(restoreConversationId()).toBe(CONVERSATION);
    expect(storage.getItem('enterprise-agent.knowledge-session')).toContain('旧缓存');
    expect(restoreConversationId()).toBe(CONVERSATION);
  });

  it('drops a conversation id that is not a uuid', () => {
    const storage = memoryStorage();
    storage.setItem('enterprise-agent.conversation-id', 'not-a-uuid');
    vi.stubGlobal('sessionStorage', storage);

    expect(restoreConversationId()).toBeUndefined();
  });
});

describe('history messages', () => {
  it('renders server messages in order and keeps an empty history empty', () => {
    expect(turnsFromHistory([])).toEqual([]);
    const turns = turnsFromHistory([
      { role: 'user', content: '第一题', status: 'answered', sourceIds: [] },
      { role: 'assistant', content: '第一轮', status: 'answered', sourceIds: ['chunk-1'], sources: [{ chunk_id: 'chunk-1', content: '付款申请须先由部门负责人审批。' }] },
      { role: 'user', content: '第二题', status: 'answered', sourceIds: [] },
      { role: 'assistant', content: '第二轮', status: 'answered', sourceIds: ['chunk-2'], sources: [{ content: '' }] },
    ]);

    expect(turns.map((turn) => turn.question)).toEqual(['第一题', '第二题']);
    expect(turns.map((turn) => turn.run.answer)).toEqual(['第一轮', '第二轮']);
    expect(turns[0]?.run.sources).toEqual([{ documentId: '', chunkId: 'chunk-1', content: '付款申请须先由部门负责人审批。' }]);
    expect(turns[1]?.run.sources).toEqual([]);
    expect(turns[1]?.run.completed).toBe(true);
  });

  it('clears the conversation only when the server says it does not exist', () => {
    expect(historyFailureClearsConversation(404)).toBe(true);
    expect(historyFailureClearsConversation(409)).toBe(false);
    expect(historyFailureClearsConversation(503)).toBe(false);
    expect(historyFailureClearsConversation(undefined)).toBe(false);
  });
});

function memoryStorage(): Storage {
  const values = new Map<string, string>();
  return {
    get length() {
      return values.size;
    },
    clear() {
      values.clear();
    },
    getItem(key: string) {
      return values.get(key) ?? null;
    },
    key() {
      return null;
    },
    removeItem(key: string) {
      values.delete(key);
    },
    setItem(key: string, value: string) {
      values.set(key, value);
    },
  };
}
