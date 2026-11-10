import {
  AuditOutlined,
  CheckSquareOutlined,
  DashboardOutlined,
  FileTextOutlined,
  ReadOutlined,
} from '@ant-design/icons';
import { Layout, Menu } from 'antd';
import { Outlet, useLocation, useNavigate } from 'react-router-dom';
import { useAppStore } from '../stores/appStore';

const { Header, Sider, Content } = Layout;

const menuItems = [
  { key: '/dashboard', icon: <DashboardOutlined />, label: '工作台' },
  { key: '/contracts', icon: <FileTextOutlined />, label: '合同管理' },
  { key: '/reviews', icon: <AuditOutlined />, label: '智能审核' },
  { key: '/approvals', icon: <CheckSquareOutlined />, label: '审批中心' },
  { key: '/knowledge', icon: <ReadOutlined />, label: '知识库' },
];

export default function MainLayout() {
  const navigate = useNavigate();
  const location = useLocation();
  const collapsed = useAppStore((state) => state.sidebarCollapsed);
  const setSidebarCollapsed = useAppStore((state) => state.setSidebarCollapsed);
  const selectedKey = menuItems.some((item) => item.key === location.pathname) ? location.pathname : '';

  return (
    <Layout style={{ minHeight: '100%' }}>
      <Sider collapsible collapsed={collapsed} onCollapse={setSidebarCollapsed} theme="dark">
        <div
          style={{
            height: 64,
            display: 'flex',
            alignItems: 'center',
            justifyContent: collapsed ? 'center' : 'flex-start',
            paddingInline: collapsed ? 0 : 24,
            color: '#fff',
            fontWeight: 600,
          }}
        >
          {collapsed ? 'EA' : 'Enterprise Agent'}
        </div>
        <Menu
          theme="dark"
          mode="inline"
          selectedKeys={selectedKey ? [selectedKey] : []}
          items={menuItems}
          onClick={({ key }) => navigate(key)}
        />
      </Sider>
      <Layout>
        <Header style={{ background: '#fff', paddingInline: 24, fontSize: 16, fontWeight: 600 }}>
          Enterprise Agent
        </Header>
        <Content style={{ margin: 24 }}>
          <Outlet />
        </Content>
      </Layout>
    </Layout>
  );
}
