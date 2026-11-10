import axios, { type AxiosError, type InternalAxiosRequestConfig } from 'axios';

/** 空字符串表示同源请求。开发环境由 Vite 代理到 Java，生产环境由网关转发。 */
const baseURL = import.meta.env.VITE_API_BASE_URL ?? '';

type TokenReader = () => string | undefined;

let readAccessToken: TokenReader = () => undefined;

/** 后续登录态只放内存，由调用方注册读取函数。这里不保存、不硬编码令牌。 */
export function setAccessTokenReader(reader: TokenReader): void {
  readAccessToken = reader;
}

export class RequestError extends Error {
  readonly status?: number;

  constructor(message: string, status?: number) {
    super(message);
    this.name = 'RequestError';
    this.status = status;
  }
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
  return config;
});

request.interceptors.response.use(
  (response) => response,
  (error: AxiosError<unknown>) => Promise.reject(toRequestError(error)),
);

function toRequestError(error: AxiosError<unknown>): RequestError {
  if (error.response) {
    if (isEmptyBody(error.response.data)) {
      return new RequestError('网络异常，无法连接服务');
    }
    return new RequestError(readErrorMessage(error.response.data, error.response.status), error.response.status);
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
