import { archiveMemory, createMemory, deleteMemory, updateMemory } from '../api/memories';
import { RequestError } from '../api/request';
import type { MemoryDraft } from '../pages/memory/MemoryEditor';
import type { MemoryView } from './catalog';

export interface MemoryWriteResult {
  refresh: true;
  conflict: boolean;
}

/** 写入成功或版本冲突后都重新拉取列表。冲突不会再次提交。 */
export async function commitMemory(editing: MemoryView | undefined, draft: MemoryDraft): Promise<MemoryWriteResult> {
  try {
    if (editing) {
      await updateMemory(editing, draft.content);
    } else {
      await createMemory(draft);
    }
    return { refresh: true, conflict: false };
  } catch (error: unknown) {
    if (error instanceof RequestError && error.status === 409) {
      return { refresh: true, conflict: true };
    }
    throw error;
  }
}

export async function commitArchive(memory: MemoryView): Promise<MemoryWriteResult> {
  await archiveMemory(memory);
  return { refresh: true, conflict: false };
}

export async function commitDelete(memory: MemoryView): Promise<MemoryWriteResult> {
  await deleteMemory(memory);
  return { refresh: true, conflict: false };
}
