import { Card, Col, Row, Tag, Typography } from 'antd';

const capabilities = ['合同审核', '知识检索', '智能审批'];

const modules = [
  { name: '工作台', status: '可用' },
  { name: '合同管理', status: '开发中' },
  { name: '智能审核', status: '开发中' },
  { name: '审批中心', status: '开发中' },
  { name: '知识库', status: '开发中' },
];

export default function DashboardPage() {
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
          <Card title="当前服务">
            <Typography.Paragraph style={{ marginBottom: 0 }}>前端已启动。</Typography.Paragraph>
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
