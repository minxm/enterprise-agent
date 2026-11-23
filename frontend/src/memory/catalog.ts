import { RequestError } from '../api/request';

export const MEMORY_SCOPES = ['user', 'tenant'] as const;
export const MEMORY_TYPES = ['preference', 'format', 'work_scope', 'organization_practice'] as const;
export const MEMORY_STATUSES = ['active', 'archived', 'deleted', 'expired'] as const;
export const MEMORY_CONTENT_LIMIT = 1000;

export type MemoryScope = (typeof MEMORY_SCOPES)[number];
export type MemoryType = (typeof MEMORY_TYPES)[number];
export type MemoryStatus = (typeof MEMORY_STATUSES)[number];

export interface MemoryView {
  id: string;
  scope: MemoryScope | string;
  memoryType: MemoryType | string;
  content: string;
  source: string;
  sourceConversationId?: string | null;
  status: MemoryStatus | string;
  createdAt?: string | null;
  updatedAt?: string | null;
  expiresAt?: string | null;
  deletedAt?: string | null;
  version: number;
}

export interface MemoryFilter {
  scope?: MemoryScope;
  memoryType?: MemoryType;
  status?: MemoryStatus;
  includeExpired: boolean;
}

export const EMPTY_MEMORY_FILTER: MemoryFilter = { includeExpired: false };

const SCOPE_LABELS: Record<MemoryScope, string> = {
  user: '个人',
  tenant: '租户',
};

const TYPE_LABELS: Record<MemoryType, string> = {
  preference: '偏好',
  format: '格式',
  work_scope: '工作范围',
  organization_practice: '组织习惯',
};

const STATUS_LABELS: Record<MemoryStatus, string> = {
  active: '有效',
  archived: '已归档',
  deleted: '已删除',
  expired: '已过期',
};

export const ARCHIVE_CONFIRM = '归档后，这条记忆不会再作为有效记忆使用。';
export const DELETE_CONFIRM = '删除后，记忆正文会被清空，并且不能继续作为有效记忆使用。';
export const VERSION_CONFLICT = '内容已被其他操作修改，请刷新后重试。';

export function memoryPageGate(token: string | undefined): 'login' | 'ready' {
  return token ? 'ready' : 'login';
}

export function scopeLabel(scope: string): string {
  return SCOPE_LABELS[scope as MemoryScope] ?? scope;
}

export function typeLabel(memoryType: string): string {
  return TYPE_LABELS[memoryType as MemoryType] ?? memoryType;
}

export function statusLabel(status: string): string {
  return STATUS_LABELS[status as MemoryStatus] ?? status;
}

export function isActiveMemory(memory: Pick<MemoryView, 'status'>): boolean {
  return memory.status === 'active';
}

export function memoryListParams(filter: MemoryFilter): Record<string, string | boolean> {
  const params: Record<string, string | boolean> = {};
  if (filter.scope) {
    params.scope = filter.scope;
  }
  if (filter.memoryType) {
    params.memoryType = filter.memoryType;
  }
  if (filter.status) {
    params.status = filter.status;
  }
  if (filter.includeExpired) {
    params.includeExpired = true;
  }
  return params;
}

export function validateMemoryContent(value: string): string | undefined {
  const text = value.trim();
  if (!text) {
    return '请填写记忆内容';
  }
  if (text.length > MEMORY_CONTENT_LIMIT) {
    return `记忆内容不能超过 ${MEMORY_CONTENT_LIMIT} 字`;
  }
  return undefined;
}

export function createMemoryBody(input: { scope: MemoryScope; memoryType: MemoryType; content: string }): {
  scope: MemoryScope;
  memoryType: MemoryType;
  content: string;
} {
  const contentError = validateMemoryContent(input.content);
  if (contentError) {
    throw new RequestError(contentError, 400);
  }
  return {
    scope: input.scope,
    memoryType: input.memoryType,
    content: input.content.trim(),
  };
}

export function updateMemoryBody(memory: Pick<MemoryView, 'version' | 'expiresAt'>, content: string): {
  expectedVersion: number;
  content: string;
  expiresAt?: string;
} {
  const contentError = validateMemoryContent(content);
  if (contentError) {
    throw new RequestError(contentError, 400);
  }
  const body: { expectedVersion: number; content: string; expiresAt?: string } = {
    expectedVersion: memory.version,
    content: content.trim(),
  };
  if (memory.expiresAt) {
    body.expiresAt = memory.expiresAt;
  }
  return body;
}

export function versionBody(memory: Pick<MemoryView, 'version'>): { expectedVersion: number } {
  return { expectedVersion: memory.version };
}

export function memoryErrorText(error: unknown): string {
  if (error instanceof RequestError) {
    if (error.status === 409) {
      return VERSION_CONFLICT;
    }
    if (error.status === 403) {
      return error.message || '无权执行该操作';
    }
    if (error.status === 404) {
      return error.message || '记忆不存在';
    }
    if (error.status === 503) {
      return error.message || '记忆服务暂时不可用';
    }
    return error.message || '请求失败';
  }
  return error instanceof Error && error.message ? error.message : '请求失败';
}

export function formatMemoryTime(value: string | null | undefined): string {
  if (!value) {
    return '—';
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }
  return parsed.toLocaleString('zh-CN', { hour12: false });
}
