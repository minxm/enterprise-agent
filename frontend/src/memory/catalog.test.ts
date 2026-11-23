import { afterEach, describe, expect, it, vi } from 'vitest';
import { rememberAccessToken, RequestError, restoreAccessToken, setAccessTokenReader } from '../api/request';
import {
  ARCHIVE_CONFIRM,
  DELETE_CONFIRM,
  EMPTY_MEMORY_FILTER,
  VERSION_CONFLICT,
  createMemoryBody,
  isActiveMemory,
  memoryErrorText,
  memoryListParams,
  memoryPageGate,
  statusLabel,
  updateMemoryBody,
  validateMemoryContent,
  versionBody,
} from './catalog';

const memory = {
  id: '00000000-0000-4000-8000-00000000d291',
  scope: 'user' as const,
  memoryType: 'preference' as const,
  content: '以后优先使用表格',
  source: 'user_explicit',
  status: 'active' as const,
  expiresAt: '2026-12-01T00:00:00Z',
  version: 3,
};

const identityKeys = [
  'tenantId',
  'tenant_id',
  'actorId',
  'actor_id',
  'userId',
  'user_id',
  'source',
  'confidence',
  'allowTenantRead',
  'allow_tenant_read',
  'allowTenantWrite',
  'allow_tenant_write',
];

afterEach(() => {
  setAccessTokenReader(() => undefined);
  vi.unstubAllGlobals();
});

describe('memory catalog', () => {
  it('omits empty filters and uses the Java query names', () => {
    expect(memoryListParams(EMPTY_MEMORY_FILTER)).toEqual({});
    expect(
      memoryListParams({
        scope: 'user',
        memoryType: 'format',
        status: 'archived',
        includeExpired: true,
      }),
    ).toEqual({
      scope: 'user',
      memoryType: 'format',
      status: 'archived',
      includeExpired: true,
    });
    expect(JSON.stringify(memoryListParams(EMPTY_MEMORY_FILTER))).not.toContain('page');
  });

  it('keeps create and update bodies inside the Java fields', () => {
    const created = createMemoryBody({
      scope: 'tenant',
      memoryType: 'organization_practice',
      content: '  租户口径  ',
    });
    expect(created).toEqual({
      scope: 'tenant',
      memoryType: 'organization_practice',
      content: '租户口径',
    });
    expect(Object.keys(created).sort()).toEqual(['content', 'memoryType', 'scope']);

    const updated = updateMemoryBody(memory, '新口径');
    expect(updated).toEqual({
      expectedVersion: 3,
      content: '新口径',
      expiresAt: '2026-12-01T00:00:00Z',
    });
    expect(versionBody(memory)).toEqual({ expectedVersion: 3 });
    for (const key of identityKeys) {
      expect(created).not.toHaveProperty(key);
      expect(updated).not.toHaveProperty(key);
    }
  });

  it('rejects blank or oversized content before a request', () => {
    expect(validateMemoryContent('   ')).toBe('请填写记忆内容');
    expect(validateMemoryContent('x'.repeat(1001))).toBe('记忆内容不能超过 1000 字');
    expect(validateMemoryContent('x'.repeat(1000))).toBeUndefined();
  });

  it('labels archived memories separately from active memories', () => {
    expect(statusLabel('archived')).toBe('已归档');
    expect(statusLabel('active')).toBe('有效');
    expect(isActiveMemory({ status: 'archived' })).toBe(false);
    expect(isActiveMemory({ status: 'active' })).toBe(true);
  });

  it('explains 403, 404, 409, and 503', () => {
    expect(memoryErrorText(new RequestError('无权管理租户记忆', 403))).toBe('无权管理租户记忆');
    expect(memoryErrorText(new RequestError('', 403))).toBe('无权执行该操作');
    expect(memoryErrorText(new RequestError('记忆不存在', 404))).toBe('记忆不存在');
    expect(memoryErrorText(new RequestError('', 404))).toBe('记忆不存在');
    expect(memoryErrorText(new RequestError('记忆版本冲突', 409))).toBe(VERSION_CONFLICT);
    expect(memoryErrorText(new RequestError('Agent 服务不可用', 503))).toBe('Agent 服务不可用');
    expect(memoryErrorText(new RequestError('', 503))).toBe('记忆服务暂时不可用');
  });

  it('asks for confirmation and blocks the page until login', () => {
    expect(ARCHIVE_CONFIRM).toContain('不会再作为有效记忆使用');
    expect(DELETE_CONFIRM).toContain('记忆正文会被清空');
    expect(memoryPageGate(undefined)).toBe('login');

    const storage = new Map<string, string>();
    vi.stubGlobal('sessionStorage', {
      getItem: (key: string) => storage.get(key) ?? null,
      setItem: (key: string, value: string) => storage.set(key, value),
    });
    rememberAccessToken('saved-token');
    setAccessTokenReader(() => undefined);

    expect(memoryPageGate(restoreAccessToken())).toBe('ready');
  });
});
