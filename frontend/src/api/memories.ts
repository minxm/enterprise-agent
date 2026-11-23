import type { ApiResponse } from '../types/api';
import {
  createMemoryBody,
  memoryListParams,
  updateMemoryBody,
  versionBody,
  type MemoryFilter,
  type MemoryScope,
  type MemoryType,
  type MemoryView,
} from '../memory/catalog';
import { RequestError, request } from './request';

export const MEMORIES_PATH = '/api/v1/agent/memories';

export function memoryPath(memoryId: string): string {
  return `${MEMORIES_PATH}/${memoryId}`;
}

export function archiveMemoryPath(memoryId: string): string {
  return `${memoryPath(memoryId)}/archive`;
}

export async function fetchMemories(filter: MemoryFilter): Promise<MemoryView[]> {
  const response = await request.get<ApiResponse<{ items?: MemoryView[] }>>(MEMORIES_PATH, {
    params: memoryListParams(filter),
  });
  return response.data.data?.items ?? [];
}

export async function createMemory(input: {
  scope: MemoryScope;
  memoryType: MemoryType;
  content: string;
}): Promise<MemoryView> {
  const response = await request.post<ApiResponse<MemoryView>>(MEMORIES_PATH, createMemoryBody(input));
  return requireMemory(response.data, '没有返回记忆');
}

export async function updateMemory(memory: MemoryView, content: string): Promise<MemoryView> {
  const response = await request.put<ApiResponse<MemoryView>>(memoryPath(memory.id), updateMemoryBody(memory, content));
  return requireMemory(response.data, '没有返回记忆');
}

export async function archiveMemory(memory: MemoryView): Promise<MemoryView> {
  const response = await request.post<ApiResponse<MemoryView>>(archiveMemoryPath(memory.id), versionBody(memory));
  return requireMemory(response.data, '没有返回记忆');
}

export async function deleteMemory(memory: MemoryView): Promise<MemoryView> {
  const response = await request.delete<ApiResponse<MemoryView>>(memoryPath(memory.id), {
    data: versionBody(memory),
  });
  return requireMemory(response.data, '没有返回记忆');
}

function requireMemory(body: ApiResponse<MemoryView>, fallback: string): MemoryView {
  if (!body.data?.id) {
    throw new RequestError(body.message || fallback, body.code);
  }
  return body.data;
}
