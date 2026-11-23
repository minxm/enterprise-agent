import { Form, Input, Modal, Select } from 'antd';
import { useEffect } from 'react';
import {
  MEMORY_CONTENT_LIMIT,
  MEMORY_SCOPES,
  MEMORY_TYPES,
  scopeLabel,
  typeLabel,
  validateMemoryContent,
  type MemoryScope,
  type MemoryType,
  type MemoryView,
} from '../../memory/catalog';

export interface MemoryDraft {
  scope: MemoryScope;
  memoryType: MemoryType;
  content: string;
}

interface MemoryEditorProps {
  open: boolean;
  memory?: MemoryView;
  submitting: boolean;
  onCancel: () => void;
  onSubmit: (draft: MemoryDraft) => void;
}

export default function MemoryEditor({ open, memory, submitting, onCancel, onSubmit }: MemoryEditorProps) {
  const [form] = Form.useForm<MemoryDraft>();
  const editing = Boolean(memory);

  useEffect(() => {
    if (!open) {
      return;
    }
    form.setFieldsValue({
      scope: memory?.scope === 'tenant' ? 'tenant' : 'user',
      memoryType: isMemoryType(memory?.memoryType) ? memory.memoryType : 'preference',
      content: memory?.content ?? '',
    });
  }, [form, memory, open]);

  return (
    <Modal
      title={editing ? '修改记忆' : '新建记忆'}
      open={open}
      okText={editing ? '保存' : '创建'}
      cancelText="取消"
      confirmLoading={submitting}
      destroyOnHidden
      onCancel={onCancel}
      onOk={() => {
        void form.validateFields().then((values) => onSubmit(values));
      }}
    >
      <Form form={form} layout="vertical" requiredMark={false}>
        <Form.Item name="scope" label="作用范围" rules={[{ required: true, message: '选择作用范围' }]}>
          <Select
            disabled={editing}
            options={MEMORY_SCOPES.map((scope) => ({ value: scope, label: scopeLabel(scope) }))}
          />
        </Form.Item>
        <Form.Item name="memoryType" label="记忆类型" rules={[{ required: true, message: '选择记忆类型' }]}>
          <Select
            disabled={editing}
            options={MEMORY_TYPES.map((memoryType) => ({ value: memoryType, label: typeLabel(memoryType) }))}
          />
        </Form.Item>
        <Form.Item
          name="content"
          label="记忆内容"
          rules={[
            {
              validator: async (_rule, value: string) => {
                const message = validateMemoryContent(value ?? '');
                if (message) {
                  throw new Error(message);
                }
              },
            },
          ]}
        >
          <Input.TextArea rows={4} maxLength={MEMORY_CONTENT_LIMIT} showCount placeholder="写下希望以后记住的偏好" />
        </Form.Item>
      </Form>
    </Modal>
  );
}

function isMemoryType(value: string | undefined): value is MemoryType {
  return MEMORY_TYPES.some((item) => item === value);
}
