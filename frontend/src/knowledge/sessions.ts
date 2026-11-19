export interface ConversationSummary {
  conversationId: string;
  createdAt?: string;
}

export interface OpenView {
  generation: number;
  conversationId: string | undefined;
}

export interface StreamLease {
  generation: number;
  conversationId: string;
  turnId: string;
}

export type HistoryLoadFailure = 'missing' | 'network' | 'service';

export function conversationListParams(page: number, size: number): { page: number; size: number } {
  return { page, size };
}

export function conversationLabel(item: ConversationSummary): string {
  const suffix = item.conversationId.slice(0, 8);
  const stamp = formatCreatedAt(item.createdAt);
  return stamp ? `${stamp} · ${suffix}` : suffix;
}

export function rememberCreatedConversation(
  items: ConversationSummary[],
  created: ConversationSummary,
): ConversationSummary[] {
  if (items.some((item) => item.conversationId === created.conversationId)) {
    return items;
  }
  return [created, ...items];
}

export function appendConversationPage(current: ConversationSummary[], next: ConversationSummary[]): ConversationSummary[] {
  const seen = new Set(current.map((item) => item.conversationId));
  const added = next.filter((item) => !seen.has(item.conversationId));
  return added.length === 0 ? current : [...current, ...added];
}

export function streamEventApplies(view: OpenView, lease: StreamLease): boolean {
  return view.generation === lease.generation && view.conversationId === lease.conversationId;
}

export function applyStreamToTurns<T extends { id: string }>(
  turns: T[],
  view: OpenView,
  lease: StreamLease,
  update: (turn: T) => T,
): T[] {
  if (!streamEventApplies(view, lease)) {
    return turns;
  }
  let changed = false;
  const next = turns.map((turn) => {
    if (turn.id !== lease.turnId) {
      return turn;
    }
    changed = true;
    return update(turn);
  });
  return changed ? next : turns;
}

export function historyApplies(view: OpenView, request: { generation: number; conversationId: string }): boolean {
  return view.generation === request.generation && view.conversationId === request.conversationId;
}

export function turnsAfterHistory<T>(
  current: T[],
  view: OpenView,
  request: { generation: number; conversationId: string },
  incoming: T[],
): T[] {
  return historyApplies(view, request) ? incoming : current;
}

export function classifyHistoryFailure(status: number | undefined): HistoryLoadFailure {
  if (status === 404) {
    return 'missing';
  }
  if (status === undefined) {
    return 'network';
  }
  return 'service';
}

export function historyFailureKeepsConversation(kind: HistoryLoadFailure): boolean {
  return kind !== 'missing';
}

export function historyFailureText(kind: HistoryLoadFailure, message: string): string {
  if (kind === 'missing') {
    return '会话不存在';
  }
  if (kind === 'network') {
    return '网络异常，已保留当前会话';
  }
  return message || '会话暂时无法读取';
}

function formatCreatedAt(value: string | undefined): string {
  if (!value) {
    return '';
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return '';
  }
  const pad = (part: number) => String(part).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}
