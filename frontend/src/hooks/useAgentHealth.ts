import { useQuery } from '@tanstack/react-query';
import { fetchAgentHealth, fetchJavaHealth } from '../api/health';

const healthQuery = {
  staleTime: 10_000,
  refetchInterval: 30_000,
  retry: 1,
  refetchOnWindowFocus: false,
} as const;

export function useAgentHealth() {
  return useQuery({
    queryKey: ['integration', 'agent-health'],
    queryFn: fetchAgentHealth,
    ...healthQuery,
  });
}

export function useJavaHealth() {
  return useQuery({
    queryKey: ['integration', 'java-health'],
    queryFn: fetchJavaHealth,
    ...healthQuery,
  });
}
