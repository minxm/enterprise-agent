import { afterEach, describe, expect, it, vi } from 'vitest';
import { commitArchive, commitDelete, commitMemory } from '../memory/actions';
import { EMPTY_MEMORY_FILTER, type MemoryView } from '../memory/catalog';
import {
  MEMORIES_PATH,
  archiveMemory,
  archiveMemoryPath,
  createMemory,
  deleteMemory,
  fetchMemories,
  memoryPath,
  updateMemory,
} from './memories';
import { RequestError, request } from './request';

const memory: MemoryView = {
  id: '00000000-0000-4000-8000-00000000d291',
  scope: 'user',
  memoryType: 'preference',
  content: '以后优先使用表格',
  source: 'user_explicit',
  status: 'active',
  createdAt: '2026-01-02T00:00:00Z',
  updatedAt: '2026-01-02T00:00:00Z',
  expiresAt: null,
  version: 3,
};

function ok<T>(data: T) {
  return { data: { code: 0, message: 'ok', data } };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('memory java client', () => {
  it('loads the current list from the Java route', async () => {
    const get = vi.spyOn(request, 'get').mockResolvedValue(
      ok({
        items: [memory],
      }),
    );

    await expect(fetchMemories({ ...EMPTY_MEMORY_FILTER, scope: 'user', status: 'active' })).resolves.toEqual([
      memory,
    ]);
    expect(get).toHaveBeenCalledWith(MEMORIES_PATH, {
      params: { scope: 'user', status: 'active' },
    });
    expect(MEMORIES_PATH).toBe('/api/v1/agent/memories');
    expect(MEMORIES_PATH).not.toContain('/internal/');
    expect(MEMORIES_PATH).not.toContain('8000');
  });

  it('returns an empty list when Java has no items', async () => {
    vi.spyOn(request, 'get').mockResolvedValue(ok({ items: [] }));

    await expect(fetchMemories(EMPTY_MEMORY_FILTER)).resolves.toEqual([]);
  });

  it('creates a personal memory with only the allowed fields', async () => {
    const post = vi.spyOn(request, 'post').mockResolvedValue(ok({ ...memory, version: 1 }));

    await createMemory({ scope: 'user', memoryType: 'preference', content: '以后优先使用表格' });

    expect(post).toHaveBeenCalledWith(MEMORIES_PATH, {
      scope: 'user',
      memoryType: 'preference',
      content: '以后优先使用表格',
    });
  });

  it('sends a tenant memory without a client permission flag', async () => {
    const post = vi.spyOn(request, 'post').mockRejectedValue(new RequestError('无权管理租户记忆', 403));

    await expect(
      createMemory({ scope: 'tenant', memoryType: 'organization_practice', content: '租户口径' }),
    ).rejects.toMatchObject({ status: 403, message: '无权管理租户记忆' });
    const body = post.mock.calls[0]?.[1] as Record<string, unknown>;
    expect(body).toEqual({
      scope: 'tenant',
      memoryType: 'organization_practice',
      content: '租户口径',
    });
    expect(body).not.toHaveProperty('allowTenantWrite');
    expect(post).toHaveBeenCalledTimes(1);
  });

  it('updates with the server version', async () => {
    const put = vi.spyOn(request, 'put').mockResolvedValue(ok({ ...memory, content: '新口径', version: 4 }));

    await expect(updateMemory(memory, '新口径')).resolves.toMatchObject({ version: 4, content: '新口径' });
    expect(put).toHaveBeenCalledWith(memoryPath(memory.id), {
      expectedVersion: 3,
      content: '新口径',
    });
  });

  it('reports a version conflict without submitting again', async () => {
    const put = vi.spyOn(request, 'put').mockRejectedValue(new RequestError('记忆版本冲突', 409));

    await expect(
      commitMemory(memory, { scope: 'user', memoryType: 'preference', content: '新口径' }),
    ).resolves.toEqual({ refresh: true, conflict: true });
    expect(put).toHaveBeenCalledTimes(1);
  });

  it('archives through the Java archive route and refreshes', async () => {
    const post = vi.spyOn(request, 'post').mockResolvedValue(ok({ ...memory, status: 'archived', version: 4 }));

    await expect(commitArchive(memory)).resolves.toEqual({ refresh: true, conflict: false });
    expect(post).toHaveBeenCalledWith(archiveMemoryPath(memory.id), { expectedVersion: 3 });
    expect(archiveMemoryPath(memory.id)).toBe(`${MEMORIES_PATH}/${memory.id}/archive`);
  });

  it('deletes through the Java route and asks the caller to refresh', async () => {
    const remove = vi.spyOn(request, 'delete').mockResolvedValue(ok({ ...memory, status: 'deleted', content: '' }));

    await expect(commitDelete(memory)).resolves.toEqual({ refresh: true, conflict: false });
    expect(remove).toHaveBeenCalledWith(memoryPath(memory.id), { data: { expectedVersion: 3 } });
    await expect(deleteMemory(memory)).resolves.toMatchObject({ status: 'deleted' });
  });

  it('leaves 404 and 503 on the request error', async () => {
    vi.spyOn(request, 'get').mockRejectedValueOnce(new RequestError('记忆不存在', 404));
    await expect(fetchMemories(EMPTY_MEMORY_FILTER)).rejects.toMatchObject({ status: 404, message: '记忆不存在' });

    vi.spyOn(request, 'post').mockRejectedValueOnce(new RequestError('Agent 服务不可用', 503));
    await expect(archiveMemory(memory)).rejects.toMatchObject({ status: 503, message: 'Agent 服务不可用' });
  });
});
