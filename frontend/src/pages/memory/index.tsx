import { Button, Card, Form, Input, Modal, Select, Space, Table, Tag, Typography, Alert } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useCallback, useEffect, useState } from 'react';
import { fetchMemories } from '../../api/memories';
import { rememberAccessToken, restoreAccessToken } from '../../api/request';
import { commitArchive, commitDelete, commitMemory } from '../../memory/actions';
import {
  ARCHIVE_CONFIRM,
  DELETE_CONFIRM,
  EMPTY_MEMORY_FILTER,
  VERSION_CONFLICT,
  MEMORY_SCOPES,
  MEMORY_STATUSES,
  MEMORY_TYPES,
  formatMemoryTime,
  isActiveMemory,
  memoryErrorText,
  memoryPageGate,
  scopeLabel,
  statusLabel,
  typeLabel,
  type MemoryFilter,
  type MemoryScope,
  type MemoryStatus,
  type MemoryType,
  type MemoryView,
} from '../../memory/catalog';
import MemoryEditor, { type MemoryDraft } from './MemoryEditor';

export default function MemoryPage() {
  const [token, setToken] = useState<string | undefined>(() => restoreAccessToken());
  const [filter, setFilter] = useState<MemoryFilter>(EMPTY_MEMORY_FILTER);
  const [items, setItems] = useState<MemoryView[]>([]);
  const [loading, setLoading] = useState(() => memoryPageGate(token) === 'ready');
  const [pageError, setPageError] = useState<string>();
  const [editorOpen, setEditorOpen] = useState(false);
  const [editing, setEditing] = useState<MemoryView>();
  const [submitting, setSubmitting] = useState(false);

  const load = useCallback(async (nextFilter: MemoryFilter) => {
    setLoading(true);
    try {
      const memories = await fetchMemories(nextFilter);
      setItems(memories);
      setPageError(undefined);
      return true;
    } catch (error: unknown) {
      setItems([]);
      setPageError(memoryErrorText(error));
      return false;
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (memoryPageGate(token) === 'login') {
      setItems([]);
      setLoading(false);
      return;
    }
    void load(filter);
  }, [filter, load, token]);

  const login = async (values: { tenantCode: string; username: string; password: string }) => {
    setPageError(undefined);
    const response = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Request-ID': crypto.randomUUID() },
      body: JSON.stringify(values),
    });
    const body = (await response.json()) as { message?: string; data?: { token?: string } };
    if (!response.ok || !body.data?.token) {
      setPageError(body.message || '登录失败');
      return;
    }
    rememberAccessToken(body.data.token);
    setToken(body.data.token);
  };

  const submit = async (draft: MemoryDraft) => {
    setSubmitting(true);
    try {
      const result = await commitMemory(editing, draft);
      setEditorOpen(false);
      setEditing(undefined);
      const loaded = await load(filter);
      if (result.conflict && loaded) {
        setPageError(VERSION_CONFLICT);
      }
    } catch (error: unknown) {
      setPageError(memoryErrorText(error));
    } finally {
      setSubmitting(false);
    }
  };

  const confirmArchive = (memory: MemoryView) => {
    Modal.confirm({
      title: '归档这条记忆？',
      content: ARCHIVE_CONFIRM,
      okText: '归档',
      cancelText: '取消',
      onOk: async () => {
        try {
          await commitArchive(memory);
          await load(filter);
        } catch (error: unknown) {
          setPageError(memoryErrorText(error));
        }
      },
    });
  };

  const confirmDelete = (memory: MemoryView) => {
    Modal.confirm({
      title: '删除这条记忆？',
      content: DELETE_CONFIRM,
      okText: '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        try {
          await commitDelete(memory);
          await load(filter);
        } catch (error: unknown) {
          setPageError(memoryErrorText(error));
        }
      },
    });
  };

  const columns: ColumnsType<MemoryView> = [
    { title: '内容', dataIndex: 'content', ellipsis: true },
    { title: '类型', dataIndex: 'memoryType', width: 120, render: (value: string) => typeLabel(value) },
    { title: '范围', dataIndex: 'scope', width: 100, render: (value: string) => scopeLabel(value) },
    {
      title: '状态',
      dataIndex: 'status',
      width: 110,
      render: (value: string) => <Tag color={value === 'active' ? 'green' : 'default'}>{statusLabel(value)}</Tag>,
    },
    {
      title: '更新时间',
      dataIndex: 'updatedAt',
      width: 190,
      render: (value: string | null | undefined) => formatMemoryTime(value),
    },
    {
      title: '操作',
      key: 'actions',
      width: 180,
      render: (_value, memory) => (
        <Space>
          <Button
            type="link"
            disabled={memory.status === 'deleted'}
            onClick={() => {
              setEditing(memory);
              setEditorOpen(true);
            }}
          >
            修改
          </Button>
          <Button type="link" disabled={!isActiveMemory(memory)} onClick={() => confirmArchive(memory)}>
            归档
          </Button>
          <Button type="link" danger disabled={memory.status === 'deleted'} onClick={() => confirmDelete(memory)}>
            删除
          </Button>
        </Space>
      ),
    },
  ];

  return (
    <Space direction="vertical" size={16} style={{ display: 'flex' }}>
      <Typography.Title level={3} style={{ margin: 0 }}>
        长期记忆
      </Typography.Title>
      <Typography.Paragraph type="secondary" style={{ margin: 0 }}>
        这里管理你主动保存的偏好。个人记忆只属于当前用户；租户记忆是否可以创建，由服务端权限决定。
      </Typography.Paragraph>
      {pageError && memoryPageGate(token) === 'login' ? <Alert type="error" showIcon message={pageError} /> : null}
      {memoryPageGate(token) === 'login' ? (
        <Card title="登录">
          <Form<{ tenantCode: string; username: string; password: string }>
            layout="inline"
            onFinish={(values) => void login(values)}
          >
            <Form.Item name="tenantCode" rules={[{ required: true, message: '填写租户编码' }]}>
              <Input placeholder="租户编码" />
            </Form.Item>
            <Form.Item name="username" rules={[{ required: true, message: '填写用户名' }]}>
              <Input placeholder="用户名" />
            </Form.Item>
            <Form.Item name="password" rules={[{ required: true, message: '填写密码' }]}>
              <Input.Password placeholder="密码" />
            </Form.Item>
            <Button type="primary" htmlType="submit">
              登录
            </Button>
          </Form>
        </Card>
      ) : (
        <Card>
          <Space direction="vertical" size={16} style={{ display: 'flex' }}>
            <Space wrap>
              <Select
                allowClear
                placeholder="范围"
                style={{ width: 140 }}
                value={filter.scope}
                options={MEMORY_SCOPES.map((scope) => ({ value: scope, label: scopeLabel(scope) }))}
                onChange={(scope?: MemoryScope) => setFilter((current) => ({ ...current, scope }))}
              />
              <Select
                allowClear
                placeholder="类型"
                style={{ width: 160 }}
                value={filter.memoryType}
                options={MEMORY_TYPES.map((memoryType) => ({ value: memoryType, label: typeLabel(memoryType) }))}
                onChange={(memoryType?: MemoryType) => setFilter((current) => ({ ...current, memoryType }))}
              />
              <Select
                allowClear
                placeholder="状态"
                style={{ width: 140 }}
                value={filter.status}
                options={MEMORY_STATUSES.map((status) => ({ value: status, label: statusLabel(status) }))}
                onChange={(status?: MemoryStatus) => setFilter((current) => ({ ...current, status }))}
              />
              <Button
                onClick={() => setFilter((current) => ({ ...current, includeExpired: !current.includeExpired }))}
              >
                {filter.includeExpired ? '隐藏过期' : '包含过期'}
              </Button>
              <Button
                type="primary"
                onClick={() => {
                  setEditing(undefined);
                  setEditorOpen(true);
                }}
              >
                新建记忆
              </Button>
            </Space>
            {pageError ? <Alert type="error" showIcon message={pageError} /> : null}
            <Table
              rowKey="id"
              columns={columns}
              dataSource={items}
              loading={loading}
              pagination={false}
              locale={{ emptyText: pageError ? '列表没有加载成功' : '还没有记忆' }}
            />
          </Space>
        </Card>
      )}
      <MemoryEditor
        open={editorOpen}
        memory={editing}
        submitting={submitting}
        onCancel={() => {
          setEditorOpen(false);
          setEditing(undefined);
        }}
        onSubmit={(draft) => void submit(draft)}
      />
    </Space>
  );
}
