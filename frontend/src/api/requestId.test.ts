import { describe, expect, it } from 'vitest';
import { createRequestId, extractRequestId } from './request';

describe('request id', () => {
  it('creates a safe client id', () => {
    expect(createRequestId()).toMatch(/^[A-Za-z0-9_-]{1,128}$/);
  });

  it('prefers the response header', () => {
    expect(extractRequestId('trace-header', { requestId: 'trace-body' })).toBe('trace-header');
  });

  it('falls back to the error body', () => {
    expect(extractRequestId(undefined, { requestId: 'trace-body' })).toBe('trace-body');
  });

  it('returns undefined when neither source has an id', () => {
    expect(extractRequestId(undefined, { message: '失败' })).toBeUndefined();
    expect(extractRequestId('', null)).toBeUndefined();
  });
});
