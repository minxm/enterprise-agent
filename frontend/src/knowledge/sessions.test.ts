import { describe, expect, it } from 'vitest';
import {
  appendConversationPage,
  applyStreamToTurns,
  classifyHistoryFailure,
  conversationLabel,
  conversationListParams,
  historyFailureKeepsConversation,
  historyFailureText,
  rememberCreatedConversation,
  turnsAfterHistory,
  type ConversationSummary,
} from './sessions';

const A = '00000000-0000-4000-8000-00000000c291';
const B = '00000000-0000-4000-8000-00000000c292';

describe('conversation list', () => {
  it('requests only a page and does not send a tenant, owner, or thread', () => {
    expect(conversationListParams(0, 20)).toEqual({ page: 0, size: 20 });
    expect(Object.keys(conversationListParams(1, 20))).toEqual(['page', 'size']);
  });

  it('labels a conversation with its creation time and keeps older conversations when one is created', () => {
    const older: ConversationSummary = { conversationId: A, createdAt: '2026-01-01T00:00:00Z' };
    const created: ConversationSummary = { conversationId: B, createdAt: '2026-01-02T00:00:00Z' };

    expect(conversationLabel(older)).toContain(A.slice(0, 8));
    expect(conversationLabel(older)).toContain(String(new Date(older.createdAt ?? '').getFullYear()));
    expect(conversationLabel(older)).not.toBe(A);
    expect(rememberCreatedConversation([older], created)).toEqual([created, older]);
    expect(appendConversationPage([created], [created, older])).toEqual([created, older]);
  });
});

describe('conversation switching', () => {
  const turnsA = [{ id: 'turn-a', question: 'A 的问题' }];
  const turnsB = [{ id: 'turn-b', question: 'B 的问题' }];

  it('keeps each conversation on its own messages', () => {
    const viewB = { generation: 2, conversationId: B };
    const loaded = turnsAfterHistory(turnsB, viewB, { generation: 1, conversationId: A }, turnsA);

    expect(loaded).toBe(turnsB);
    expect(turnsAfterHistory([], viewB, { generation: 2, conversationId: B }, turnsB)).toEqual(turnsB);
  });

  it('drops an SSE event that belongs to the conversation the user left', () => {
    const viewB = { generation: 2, conversationId: B };
    const leaseA = { generation: 1, conversationId: A, turnId: 'turn-a' };

    const next = applyStreamToTurns(turnsB, viewB, leaseA, () => {
      throw new Error('旧会话事件不应写入当前会话');
    });

    expect(next).toBe(turnsB);
  });

  it('applies an SSE event only to the turn in the conversation that owns it', () => {
    const viewA = { generation: 1, conversationId: A };
    const turns = [
      { id: 'turn-a', answer: '' },
      { id: 'turn-other', answer: '保持不变' },
    ];

    const next = applyStreamToTurns(turns, viewA, { generation: 1, conversationId: A, turnId: 'turn-a' }, (turn) => ({
      ...turn,
      answer: 'A 的回答',
    }));

    expect(next[0]?.answer).toBe('A 的回答');
    expect(next[1]?.answer).toBe('保持不变');
  });

  it('distinguishes a missing conversation, a network failure, and a service failure', () => {
    expect(classifyHistoryFailure(404)).toBe('missing');
    expect(classifyHistoryFailure(undefined)).toBe('network');
    expect(classifyHistoryFailure(503)).toBe('service');
    expect(classifyHistoryFailure(409)).toBe('service');
    expect(historyFailureKeepsConversation('missing')).toBe(false);
    expect(historyFailureKeepsConversation('network')).toBe(true);
    expect(historyFailureKeepsConversation('service')).toBe(true);
    expect(historyFailureText('missing', '会话不存在')).toBe('会话不存在');
    expect(historyFailureText('network', '网络异常，无法连接服务')).toBe('网络异常，已保留当前会话');
    expect(historyFailureText('service', '工作流状态暂时不可用')).toBe('工作流状态暂时不可用');
  });
});
