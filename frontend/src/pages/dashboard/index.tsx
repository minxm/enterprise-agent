import { useAgentHealth, useJavaHealth } from '../../hooks/useAgentHealth';
import { RequestError } from '../../api/request';
import type { UseQueryResult } from '@tanstack/react-query';
import { Button, Card, Col, Row, Tag, Typography } from 'antd';

const capabilities = ['合同审核', '知识检索', '智能审批'];

const modules = [
  { name: '工作台', status: '可用' },
  { name: '合同管理', status: '开发中' },
  { name: '智能审核', status: '开发中' },
  { name: '审批中心', status: '开发中' },
  { name: '知识库', status: '开发中' },
];

type StatusTone = 'success' | 'error' | 'processing';

interface StatusView {
  label: string;
  tone: StatusTone;
  detail?: string;
}

export default function DashboardPage() {
  const javaHealth = useJavaHealth();
  const agentHealth = useAgentHealth();
  const refreshing = javaHealth.isFetching || agentHealth.isFetching;
  const lastCheckedAt = Math.max(
    javaHealth.dataUpdatedAt,
    javaHealth.errorUpdatedAt,
    agentHealth.dataUpdatedAt,
    agentHealth.errorUpdatedAt,
  );

  const refresh = () => {
    void javaHealth.refetch();
    void agentHealth.refetch();
  };

  return (
    <div>
      <Typography.Title level={3} style={{ marginTop: 0 }}>
        Enterprise Agent
      </Typography.Title>
      <Typography.Paragraph type="secondary">企业级智能业务流程 Agent 平台</Typography.Paragraph>
      <Row gutter={[16, 16]}>
        <Col xs={24} lg={12}>
          <Card title="核心能力">
            {capabilities.map((item) => (
              <Tag key={item} color="blue" style={{ marginBottom: 8 }}>
                {item}
              </Tag>
            ))}
          </Card>
        </Col>
        <Col xs={24} lg={12}>
          <Card
            title="服务状态"
            extra={
              <Button onClick={refresh} loading={refreshing}>
                刷新
              </Button>
            }
          >
            <StatusRow name="React 前端" view={{ label: '正常', tone: 'success', detail: '当前页面正常运行' }} />
            <StatusRow name="Spring Boot" view={describeJava(javaHealth)} />
            <StatusRow name="Python Agent Runtime" view={describeAgent(agentHealth, javaHealth.error)} />
            <Typography.Text type="secondary">最后检测时间：{formatCheckedAt(lastCheckedAt)}</Typography.Text>
          </Card>
        </Col>
        <Col span={24}>
          <Card title="功能状态">
            {modules.map((item) => (
              <div key={item.name} style={{ display: 'flex', justifyContent: 'space-between', padding: '8px 0' }}>
                <span>{item.name}</span>
                <Tag color={item.status === '可用' ? 'success' : 'default'}>{item.status}</Tag>
              </div>
            ))}
          </Card>
        </Col>
      </Row>
    </div>
  );
}

function StatusRow({ name, view }: { name: string; view: StatusView }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', gap: 16, padding: '8px 0' }}>
      <div>
        <div>{name}</div>
        {view.detail ? <Typography.Text type="secondary">{view.detail}</Typography.Text> : null}
      </div>
      <Tag color={view.tone}>{view.label}</Tag>
    </div>
  );
}

function describeJava(query: UseQueryResult<{ status: string }, Error>): StatusView {
  if (query.isPending) {
    return { label: '检测中', tone: 'processing' };
  }
  if (query.isError) {
    return failureView(query.error);
  }
  if (query.data?.status === 'UP') {
    return { label: '正常', tone: 'success', detail: 'Actuator status=UP' };
  }
  return { label: '异常', tone: 'error', detail: `Actuator status=${query.data?.status ?? 'unknown'}` };
}

function describeAgent(query: UseQueryResult<{ status: string; service: string }, Error>, javaError: Error | null): StatusView {
  if (isConnectionFailure(javaError)) {
    return { label: '无法检测', tone: 'error', detail: '未能连接 Spring Boot，无法确认 Agent Runtime' };
  }
  if (query.isPending) {
    return { label: '检测中', tone: 'processing' };
  }
  if (query.isError) {
    return failureView(query.error);
  }
  if (query.data?.status === 'ok') {
    return { label: '正常', tone: 'success', detail: `${query.data.service}（${query.data.status}）` };
  }
  return { label: '异常', tone: 'error', detail: `status=${query.data?.status ?? 'unknown'}` };
}

function failureView(error: unknown): StatusView {
  if (error instanceof RequestError && error.status === undefined) {
    return { label: '连接失败', tone: 'error', detail: error.message };
  }
  if (error instanceof RequestError) {
    return { label: '异常', tone: 'error', detail: error.message };
  }
  return { label: '异常', tone: 'error', detail: '检测失败' };
}

function isConnectionFailure(error: unknown): boolean {
  return error instanceof RequestError && error.status === undefined;
}

function formatCheckedAt(timestamp: number): string {
  if (timestamp <= 0) {
    return '尚未检测';
  }
  return new Date(timestamp).toLocaleString('zh-CN', { hour12: false });
}
