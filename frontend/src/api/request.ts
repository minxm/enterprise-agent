import axios, { type AxiosError, type InternalAxiosRequestConfig } from 'axios';

/** 空字符串表示同源请求。开发环境由 Vite 代理到 Java，生产环境由网关转发。 */
const baseURL = import.meta.env.VITE_API_BASE_URL ?? '';

const ACCESS_TOKEN_STORAGE_KEY = 'enterprise-agent.access-token';

type TokenReader = () => string | undefined;

let readAccessToken: TokenReader = () => undefined;

/** 调用方可以替换读取函数。这里不硬编码令牌。 */
export function setAccessTokenReader(reader: TokenReader): void {
  readAccessToken = reader;
}

/** 登录成功后写入 sessionStorage，并让后续请求读到这份 token。 */
export function rememberAccessToken(token: string): void {
  writeStoredAccessToken(token);
  setAccessTokenReader(() => token);
}

/** 页面重新加载后从 sessionStorage 恢复。没有记录时读取函数返回空。 */
export function restoreAccessToken(): string | undefined {
  const token = readStoredAccessToken();
  setAccessTokenReader(() => token);
  return token;
}

export function currentAuthorization(): string | undefined {
  const token = readAccessToken();
  return token ? `Bearer ${token}` : undefined;
}

export class RequestError extends Error {
  readonly status?: number;
  readonly code?: number;
  readonly requestId?: string;

  constructor(message: string, status?: number, code?: number, requestId?: string) {
    super(message);
    this.name = 'RequestError';
    this.status = status;
    this.code = code;
    this.requestId = requestId;
  }
}

const SAFE_REQUEST_ID = /^[A-Za-z0-9_-]{1,128}$/;

export function createRequestId(): string {
  const generated = globalThis.crypto?.randomUUID?.() ?? `req-${Date.now().toString(36)}`;
  return SAFE_REQUEST_ID.test(generated) ? generated : `req-${Date.now().toString(36)}`;
}

export function extractRequestId(headerValue: unknown, body: unknown): string | undefined {
  const fromHeader = headerText(headerValue);
  if (fromHeader) {
    return fromHeader;
  }
  if (isRecord(body) && typeof body.requestId === 'string' && body.requestId.length > 0) {
    return body.requestId;
  }
  return undefined;
}

export const request = axios.create({
  baseURL,
  timeout: 15_000,
});

request.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const token = readAccessToken();
  if (token) {
    config.headers.set('Authorization', `Bearer ${token}`);
  }
  if (!config.headers.has('X-Request-ID')) {
    config.headers.set('X-Request-ID', createRequestId());
  }
  return config;
});

request.interceptors.response.use(
  (response) => response,
  (error: AxiosError<unknown>) => Promise.reject(toRequestError(error)),
);

function toRequestError(error: AxiosError<unknown>): RequestError {
  if (error.response) {
    const requestId = extractRequestId(error.response.headers['x-request-id'], error.response.data);
    if (isEmptyBody(error.response.data)) {
      return new RequestError('网络异常，无法连接服务', undefined, undefined, requestId);
    }
    return new RequestError(
      readErrorMessage(error.response.data, error.response.status),
      error.response.status,
      readErrorCode(error.response.data, error.response.status),
      requestId,
    );
  }
  if (error.code === 'ECONNABORTED') {
    return new RequestError('请求超时');
  }
  return new RequestError('网络异常，无法连接服务');
}

function readErrorMessage(data: unknown, status: number): string {
  if (isRecord(data) && typeof data.message === 'string' && data.message.length > 0) {
    return data.message;
  }
  return `请求失败 (${status})`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function isEmptyBody(data: unknown): boolean {
  return data == null || (typeof data === 'string' && data.trim() === '');
}

function readErrorCode(data: unknown, status: number): number {
  if (isRecord(data) && typeof data.code === 'number') {
    return data.code;
  }
  return status;
}

function headerText(value: unknown): string | undefined {
  const candidate = Array.isArray(value) ? value[0] : value;
  return typeof candidate === 'string' && candidate.length > 0 ? candidate : undefined;
}

function readStoredAccessToken(): string | undefined {
  try {
    const value = globalThis.sessionStorage?.getItem(ACCESS_TOKEN_STORAGE_KEY);
    return value && value.length > 0 ? value : undefined;
  } catch {
    return undefined;
  }
}

function writeStoredAccessToken(token: string): void {
  try {
    globalThis.sessionStorage?.setItem(ACCESS_TOKEN_STORAGE_KEY, token);
  } catch {
    // 浏览器禁止存储时仍保留本次内存中的登录态。
  }
}
