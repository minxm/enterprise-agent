export interface Citation {
  documentId: string;
  chunkId: string;
  content: string;
}

export interface WorkflowNode {
  name: string;
  status: 'started' | 'completed';
}

export interface RunView {
  running: boolean;
  completed: boolean;
  stopped: boolean;
  nodes: WorkflowNode[];
  retrievalCount: number | null;
  requestedStrategy: string | null;
  effectiveStrategy: string | null;
  degraded: boolean;
  degradationReason: string | null;
  answer: string;
  sources: Citation[];
  error: string | null;
  requestId: string | null;
}

export function emptyRun(): RunView {
  return {
    running: false,
    completed: false,
    stopped: false,
    nodes: [],
    retrievalCount: null,
    requestedStrategy: null,
    effectiveStrategy: null,
    degraded: false,
    degradationReason: null,
    answer: '',
    sources: [],
    error: null,
    requestId: null,
  };
}

export function applyKnowledgeEvent(state: RunView, eventName: string, dataText: string): RunView {
  let data: Record<string, unknown>;
  try {
    data = JSON.parse(dataText) as Record<string, unknown>;
  } catch {
    return { ...state, running: false, error: '事件数据无法解析' };
  }
  const requestId = typeof data.request_id === 'string' ? data.request_id : state.requestId;
  if (eventName === 'run_started') {
    return { ...emptyRun(), running: true, requestId };
  }
  if (eventName === 'node_started' || eventName === 'node_completed') {
    const name = typeof data.node === 'string' ? data.node : 'unknown';
    const status = eventName === 'node_started' ? 'started' : 'completed';
    const nodes = state.nodes.filter((node) => node.name !== name);
    nodes.push({ name, status });
    return { ...state, nodes, requestId };
  }
  if (eventName === 'retrieval_completed') {
    return {
      ...state,
      requestId,
      retrievalCount: numberValue(data.retrieval_count),
      requestedStrategy: textValue(data.requested_strategy),
      effectiveStrategy: textValue(data.effective_strategy),
    };
  }
  if (eventName === 'rerank_completed') {
    return { ...state, requestId };
  }
  if (eventName === 'rerank_degraded') {
    return {
      ...state,
      requestId,
      degraded: true,
      degradationReason: textValue(data.degradation_reason) ?? '检索结果降级',
      requestedStrategy: textValue(data.requested_strategy) ?? state.requestedStrategy,
      effectiveStrategy: textValue(data.effective_strategy) ?? state.effectiveStrategy,
    };
  }
  if (eventName === 'message') {
    return {
      ...state,
      requestId,
      answer: typeof data.answer === 'string' ? data.answer : '',
      sources: readSources(data.sources),
      retrievalCount: numberValue(data.retrieval_count) ?? state.retrievalCount,
      effectiveStrategy: textValue(data.effective_strategy) ?? state.effectiveStrategy,
      degraded: data.degraded === true || state.degraded,
    };
  }
  if (eventName === 'run_completed') {
    return { ...state, running: false, completed: true, requestId };
  }
  if (eventName === 'run_failed') {
    return {
      ...state,
      running: false,
      completed: false,
      requestId,
      error: textValue(data.safe_error_message) ?? '工作流执行失败',
    };
  }
  return { ...state, requestId };
}

function readSources(value: unknown): Citation[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.flatMap((item) => {
    if (typeof item !== 'object' || item === null) {
      return [];
    }
    const source = item as Record<string, unknown>;
    return [
      {
        documentId: textValue(source.document_id) ?? '',
        chunkId: textValue(source.chunk_id) ?? '',
        content: textValue(source.content) ?? '',
      },
    ];
  });
}

function textValue(value: unknown): string | null {
  return typeof value === 'string' && value.length > 0 ? value : null;
}

function numberValue(value: unknown): number | null {
  return typeof value === 'number' ? value : null;
}
