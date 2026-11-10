import type { ApiResponse } from '../types/api';
import { RequestError, request } from './request';

/** 与 Python `GET /api/v1/health` 的字段一致，由 Java 放在 ApiResponse.data 中返回。 */
export interface AgentHealth {
  status: string;
  service: string;
}

/** Spring Boot Actuator 的进程健康结果，不代表 Python 可用。 */
export interface JavaProcessHealth {
  status: string;
}

export async function fetchAgentHealth(): Promise<AgentHealth> {
  const response = await request.get<ApiResponse<AgentHealth | null>>('/api/v1/integration/agent/health');
  const body = response.data;
  if (body.data == null) {
    throw new RequestError(body.message || 'Agent 健康检查没有返回数据', body.code);
  }
  return body.data;
}

export async function fetchJavaHealth(): Promise<JavaProcessHealth> {
  const response = await request.get<JavaProcessHealth>('/actuator/health');
  return response.data;
}
