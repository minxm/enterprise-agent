import { afterEach, describe, expect, it, vi } from 'vitest';
import { currentAuthorization, rememberAccessToken, restoreAccessToken, setAccessTokenReader } from './request';

afterEach(() => {
  setAccessTokenReader(() => undefined);
  vi.unstubAllGlobals();
});

describe('access token session', () => {
  it('keeps the token in sessionStorage and attaches it to later requests', () => {
    const storage = memoryStorage();
    vi.stubGlobal('sessionStorage', storage);

    rememberAccessToken('saved-token');

    expect(storage.getItem('enterprise-agent.access-token')).toBe('saved-token');
    expect(currentAuthorization()).toBe('Bearer saved-token');
  });

  it('restores the token after the in-memory reader is cleared', () => {
    const storage = memoryStorage();
    vi.stubGlobal('sessionStorage', storage);
    rememberAccessToken('saved-token');
    setAccessTokenReader(() => undefined);

    expect(restoreAccessToken()).toBe('saved-token');
    expect(currentAuthorization()).toBe('Bearer saved-token');
  });

  it('returns no authorization when sessionStorage is empty', () => {
    vi.stubGlobal('sessionStorage', memoryStorage());

    expect(restoreAccessToken()).toBeUndefined();
    expect(currentAuthorization()).toBeUndefined();
  });

  it('keeps the in-memory token when sessionStorage rejects writes', () => {
    vi.stubGlobal('sessionStorage', {
      getItem: () => null,
      setItem: () => {
        throw new Error('blocked');
      },
    });

    rememberAccessToken('memory-only');

    expect(currentAuthorization()).toBe('Bearer memory-only');
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
