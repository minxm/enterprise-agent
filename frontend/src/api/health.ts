import type { ApiResponse } from '../types/api';
import { request } from './request';

export interface AgentHealth {
  status: string;
  service: string;
}

/** 对应 Java `GET /api/v1/integration/agent/health`，不访问 Python。 */
export function fetchAgentHealth() {
  return request.get<ApiResponse<AgentHealth>>('/api/v1/integration/agent/health');
}
