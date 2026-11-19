import { describe, expect, it } from 'vitest';
import { applyKnowledgeEvent, emptyRun } from './events';

describe('knowledge event reducer', () => {
  it('tracks workflow progress and then the final answer', () => {
    let state = { ...emptyRun(), running: true };
    state = applyKnowledgeEvent(state, 'node_started', '{"node":"knowledge_qa","request_id":"trace-1"}');
    state = applyKnowledgeEvent(state, 'node_completed', '{"node":"knowledge_qa","status":"completed","request_id":"trace-1"}');
    state = applyKnowledgeEvent(
      state,
      'retrieval_completed',
      '{"retrieval_count":3,"requested_strategy":"vector","effective_strategy":"vector","request_id":"trace-1"}',
    );
    state = applyKnowledgeEvent(
      state,
      'message',
      '{"answer":"部门负责人审批。","sources":[{"document_id":"d1","chunk_id":"c1","content":"部门负责人审批。"}],"retrieval_count":3,"effective_strategy":"vector","degraded":false,"request_id":"trace-1"}',
    );
    state = applyKnowledgeEvent(state, 'run_completed', '{"status":"answered","request_id":"trace-1"}');

    expect(state.nodes).toEqual([{ name: 'knowledge_qa', status: 'completed' }]);
    expect(state.retrievalCount).toBe(3);
    expect(state.effectiveStrategy).toBe('vector');
    expect(state.answer).toBe('部门负责人审批。');
    expect(state.sources[0]?.chunkId).toBe('c1');
    expect(state.running).toBe(false);
    expect(state.completed).toBe(true);
    expect(state.answer).not.toContain('node_started');
  });

  it('shows a degraded retrieval and a safe failure', () => {
    let state = { ...emptyRun(), running: true };
    state = applyKnowledgeEvent(
      state,
      'rerank_degraded',
      '{"requested_strategy":"hybrid_rerank","effective_strategy":"hybrid","degradation_reason":"RerankDegraded","request_id":"trace-2"}',
    );
    expect(state.degraded).toBe(true);
    expect(state.degradationReason).toBe('RerankDegraded');

    state = applyKnowledgeEvent(
      state,
      'run_failed',
      '{"error_code":"rag_failed","safe_error_message":"RuntimeError","request_id":"trace-2"}',
    );
    expect(state.running).toBe(false);
    expect(state.error).toBe('RuntimeError');
    expect(state.completed).toBe(false);
  });

  it('stops the run when event JSON is invalid', () => {
    const state = applyKnowledgeEvent({ ...emptyRun(), running: true }, 'message', '{"answer":');
    expect(state.error).toBe('事件数据无法解析');
    expect(state.running).toBe(false);
  });
});
