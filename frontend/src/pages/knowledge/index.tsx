import { Button, Card, Form, Input, List, Select, Space, Tag, Typography } from 'antd';
import { useEffect, useRef, useState } from 'react';
import { RequestError, rememberAccessToken, restoreAccessToken } from '../../api/request';
import {
  CONVERSATION_PAGE_SIZE,
  createConversation,
  fetchConversationMessages,
  fetchConversations,
  streamKnowledgeQa,
} from '../../api/knowledgeQa';
import { applyKnowledgeEvent, emptyRun, type Citation, type RunView } from '../../knowledge/events';
import { rememberConversationId, restoreConversationId, turnsFromHistory } from '../../knowledge/history';
import {
  appendConversationPage,
  applyStreamToTurns,
  classifyHistoryFailure,
  conversationLabel,
  historyFailureKeepsConversation,
  historyFailureText,
  rememberCreatedConversation,
  turnsAfterHistory,
  type ConversationSummary,
  type OpenView,
  type StreamLease,
} from '../../knowledge/sessions';

interface Turn {
  id: string;
  question: string;
  run: RunView;
}

const STRATEGIES = [
  { value: 'vector', label: '向量检索' },
  { value: 'hybrid', label: '混合检索' },
  { value: 'hybrid_rerank', label: '混合检索并重排序' },
];

export default function KnowledgeQaPage() {
  const [token, setToken] = useState<string | undefined>(() => restoreAccessToken());
  const [restoreId] = useState<string | undefined>(() => restoreConversationId());
  const [conversationId, setConversationId] = useState<string | undefined>(restoreId);
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [conversationTotal, setConversationTotal] = useState(0);
  const [listError, setListError] = useState<string>();
  const [loadingMore, setLoadingMore] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(() => Boolean(token) && Boolean(restoreId));
  const [question, setQuestion] = useState('');
  const [strategy, setStrategy] = useState('vector');
  const [turns, setTurns] = useState<Turn[]>([]);
  const [activeId, setActiveId] = useState<string>();
  const [controller, setController] = useState<AbortController>();
  const [pageError, setPageError] = useState<string>();
  const viewRef = useRef<OpenView>({ generation: 0, conversationId: restoreId });
  const abortRef = useRef<AbortController | undefined>(undefined);
  const listRequest = useRef(0);
  const historyErrorRef = useRef(false);
  const running = turns.some((turn) => turn.run.running);

  useEffect(() => {
    if (!token) {
      setHistoryLoading(false);
      setConversations([]);
      return;
    }
    const generation = viewRef.current.generation;
    const selected = viewRef.current.conversationId;
    const request = ++listRequest.current;
    let cancelled = false;
    void fetchConversations(0, CONVERSATION_PAGE_SIZE)
      .then((page) => {
        if (cancelled || request !== listRequest.current) {
          return;
        }
        setConversations(page.items);
        setConversationTotal(page.total);
        setListError(undefined);
      })
      .catch((error: unknown) => {
        if (cancelled || request !== listRequest.current) {
          return;
        }
        setListError(error instanceof Error ? error.message : '会话列表暂时无法加载');
      });
    if (!selected) {
      setHistoryLoading(false);
      return () => {
        cancelled = true;
      };
    }
    setHistoryLoading(true);
    void fetchConversationMessages(selected)
      .then((messages) => {
        if (cancelled) {
          return;
        }
        setTurns((current) =>
          turnsAfterHistory(current, viewRef.current, { generation, conversationId: selected }, turnsFromHistory(messages)),
        );
        if (viewRef.current.generation === generation && viewRef.current.conversationId === selected) {
          setPageError(undefined);
          historyErrorRef.current = false;
        }
      })
      .catch((error: unknown) => {
        if (cancelled) {
          return;
        }
        applyHistoryFailure(selected, generation, error);
      })
      .finally(() => {
        if (!cancelled && viewRef.current.generation === generation) {
          setHistoryLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [token]);

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
    abortRef.current?.abort();
    abortRef.current = undefined;
    setController(undefined);
    viewRef.current = { generation: viewRef.current.generation + 1, conversationId: undefined };
    setConversationId(undefined);
    rememberConversationId(undefined);
    setTurns([]);
    const accessToken = body.data.token;
    rememberAccessToken(accessToken);
    setToken(accessToken);
  };

  const refreshConversations = async () => {
    const request = ++listRequest.current;
    try {
      const page = await fetchConversations(0, CONVERSATION_PAGE_SIZE);
      if (request !== listRequest.current) {
        return;
      }
      setConversations(page.items);
      setConversationTotal(page.total);
      setListError(undefined);
    } catch (error: unknown) {
      if (request !== listRequest.current) {
        return;
      }
      setListError(error instanceof Error ? error.message : '会话列表暂时无法加载');
    }
  };

  const loadMore = async () => {
    const request = ++listRequest.current;
    const pageIndex = Math.floor(conversations.length / CONVERSATION_PAGE_SIZE);
    setLoadingMore(true);
    try {
      const page = await fetchConversations(pageIndex, CONVERSATION_PAGE_SIZE);
      if (request !== listRequest.current) {
        return;
      }
      setConversations((current) => appendConversationPage(current, page.items));
      setConversationTotal(page.total);
      setListError(undefined);
    } catch (error: unknown) {
      if (request !== listRequest.current) {
        return;
      }
      setListError(error instanceof Error ? error.message : '会话列表暂时无法加载');
    } finally {
      if (request === listRequest.current) {
        setLoadingMore(false);
      }
    }
  };

  const applyHistoryFailure = (id: string, generation: number, error: unknown) => {
    if (viewRef.current.generation !== generation || viewRef.current.conversationId !== id) {
      return;
    }
    const status = error instanceof RequestError ? error.status : undefined;
    const kind = classifyHistoryFailure(status);
    historyErrorRef.current = true;
    if (!historyFailureKeepsConversation(kind)) {
      viewRef.current = { generation, conversationId: undefined };
      setConversationId(undefined);
      rememberConversationId(undefined);
      setTurns([]);
      setConversations((items) => items.filter((item) => item.conversationId !== id));
    }
    const message = error instanceof Error ? error.message : '请求失败';
    setPageError(historyFailureText(kind, message));
  };

  const loadHistory = async (id: string, generation: number) => {
    setHistoryLoading(true);
    try {
      const messages = await fetchConversationMessages(id);
      setTurns((current) =>
        turnsAfterHistory(current, viewRef.current, { generation, conversationId: id }, turnsFromHistory(messages)),
      );
      if (viewRef.current.generation === generation && viewRef.current.conversationId === id) {
        setPageError(undefined);
        historyErrorRef.current = false;
      }
    } catch (error: unknown) {
      applyHistoryFailure(id, generation, error);
    } finally {
      if (viewRef.current.generation === generation) {
        setHistoryLoading(false);
      }
    }
  };

  const openConversation = (nextId: string | undefined) => {
    abortRef.current?.abort();
    abortRef.current = undefined;
    setController(undefined);
    const generation = viewRef.current.generation + 1;
    viewRef.current = { generation, conversationId: nextId };
    setConversationId(nextId);
    rememberConversationId(nextId);
    setTurns([]);
    setActiveId(undefined);
    setPageError(undefined);
    historyErrorRef.current = false;
    if (!nextId || !token) {
      setHistoryLoading(false);
      return;
    }
    void loadHistory(nextId, generation);
  };

  const selectConversation = (id: string) => {
    if (id === viewRef.current.conversationId && !historyErrorRef.current) {
      return;
    }
    openConversation(id);
  };

  const ensureConversation = async (generationAtStart: number): Promise<string> => {
    const existing = viewRef.current.conversationId;
    if (existing) {
      return existing;
    }
    const created = await createConversation();
    const summary = { conversationId: created, createdAt: new Date().toISOString() };
    setConversations((items) => rememberCreatedConversation(items, summary));
    setConversationTotal((total) => total + 1);
    if (viewRef.current.generation !== generationAtStart) {
      throw new DOMException('The operation was aborted.', 'AbortError');
    }
    viewRef.current = { generation: generationAtStart, conversationId: created };
    setConversationId(created);
    rememberConversationId(created);
    void refreshConversations();
    return created;
  };

  const send = async () => {
    const text = question.trim();
    if (!text || running || historyLoading) {
      return;
    }
    const generationAtStart = viewRef.current.generation;
    setPageError(undefined);
    const turnId = crypto.randomUUID();
    const abort = new AbortController();
    abortRef.current?.abort();
    abortRef.current = abort;
    setController(abort);
    setActiveId(turnId);
    setTurns((current) => {
      if (viewRef.current.generation !== generationAtStart) {
        return current;
      }
      return [...current, { id: turnId, question: text, run: { ...emptyRun(), running: true } }];
    });
    setQuestion('');
    let lease: StreamLease | undefined;
    try {
      const currentConversation = await ensureConversation(generationAtStart);
      lease = { generation: generationAtStart, conversationId: currentConversation, turnId };
      if (!abort.signal.aborted && viewRef.current.generation === generationAtStart) {
        viewRef.current = { generation: generationAtStart, conversationId: currentConversation };
      }
      if (abort.signal.aborted || viewRef.current.generation !== generationAtStart) {
        return;
      }
      await streamKnowledgeQa(
        { conversationId: currentConversation, question: text, retrievalStrategy: strategy },
        (frame) => {
          const owned = lease;
          if (!owned) {
            return;
          }
          setTurns((current) =>
            applyStreamToTurns(current, viewRef.current, owned, (turn) => ({
              ...turn,
              run: applyKnowledgeEvent(turn.run, frame.event, frame.data),
            })),
          );
        },
        abort.signal,
      );
      const owned = lease;
      setTurns((current) =>
        applyStreamToTurns(current, viewRef.current, owned, (turn) =>
          turn.run.running ? { ...turn, run: { ...turn.run, running: false } } : turn,
        ),
      );
    } catch (error) {
      if (viewRef.current.generation !== generationAtStart) {
        return;
      }
      const aborted = error instanceof DOMException && error.name === 'AbortError';
      setTurns((current) =>
        current.map((turn) =>
          turn.id === turnId
            ? {
                ...turn,
                run: {
                  ...turn.run,
                  running: false,
                  stopped: aborted,
                  error: aborted ? turn.run.error : error instanceof Error ? error.message : '请求失败',
                },
              }
            : turn,
        ),
      );
    } finally {
      if (abortRef.current === abort) {
        abortRef.current = undefined;
        setController(undefined);
      }
    }
  };

  const selected = conversations.find((item) => item.conversationId === conversationId);

  return (
    <Space direction="vertical" size={16} style={{ display: 'flex' }}>
      <Typography.Title level={3} style={{ margin: 0 }}>
        知识问答
      </Typography.Title>
      <Typography.Paragraph type="secondary" style={{ margin: 0 }}>
        这里展示工作流进度。答案在本轮执行结束后一次返回，不是逐字生成。
      </Typography.Paragraph>
      {token ? null : (
        <Card title="登录">
          <Form layout="inline" onFinish={login}>
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
      )}
      <div style={{ display: 'flex', gap: 16, alignItems: 'flex-start', flexWrap: 'wrap' }}>
        <Card title={`会话${conversationTotal > 0 ? `（${conversationTotal}）` : ''}`} style={{ width: 280, flex: '0 0 280px' }}>
          <Space direction="vertical" size={8} style={{ display: 'flex' }}>
            <Button onClick={() => openConversation(undefined)} disabled={!token} block>
              新建对话
            </Button>
            {conversations.map((item) => (
              <Button
                key={item.conversationId}
                block
                type={item.conversationId === conversationId ? 'primary' : 'default'}
                onClick={() => selectConversation(item.conversationId)}
                style={{ textAlign: 'left' }}
              >
                {conversationLabel(item)}
              </Button>
            ))}
            {conversations.length === 0 ? <Typography.Text type="secondary">还没有会话</Typography.Text> : null}
            {conversations.length < conversationTotal ? (
              <Button onClick={() => void loadMore()} loading={loadingMore} disabled={!token} block>
                更早的会话
              </Button>
            ) : null}
            {listError ? <Typography.Text type="danger">{listError}</Typography.Text> : null}
          </Space>
        </Card>
        <Card
          title={selected ? conversationLabel(selected) : conversationId ? conversationLabel({ conversationId, createdAt: undefined }) : '新会话'}
          style={{ flex: '1 1 480px' }}
        >
          <Space direction="vertical" size={12} style={{ display: 'flex' }}>
            {historyLoading ? <Typography.Text type="secondary">正在读取历史消息</Typography.Text> : null}
            {turns.map((turn) => (
              <TurnView key={turn.id} turn={turn} active={turn.id === activeId} />
            ))}
            <Input.TextArea
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              placeholder="输入问题"
              autoSize={{ minRows: 2, maxRows: 4 }}
              disabled={!token || historyLoading}
            />
            <Space>
              <Select value={strategy} options={STRATEGIES} onChange={setStrategy} disabled={running || historyLoading} style={{ width: 180 }} />
              <Button type="primary" onClick={() => void send()} disabled={!token || historyLoading || running || question.trim() === ''}>
                发送
              </Button>
              <Button danger disabled={!running} onClick={() => controller?.abort()}>
                停止
              </Button>
            </Space>
            {pageError ? <Typography.Text type="danger">{pageError}</Typography.Text> : null}
          </Space>
        </Card>
      </div>
    </Space>
  );
}

function TurnView({ turn, active }: { turn: Turn; active: boolean }) {
  const run = turn.run;
  return (
    <Card size="small" title={turn.question} extra={<StatusTag run={run} active={active} />}>
      <Space direction="vertical" size={8} style={{ display: 'flex' }}>
        <div>
          {run.nodes.map((node) => (
            <Tag key={node.name} color={node.status === 'completed' ? 'success' : 'processing'}>
              {node.name} · {node.status === 'completed' ? '完成' : '执行中'}
            </Tag>
          ))}
        </div>
        {run.retrievalCount !== null ? (
          <Typography.Text>
            检索数量 {run.retrievalCount}
            {run.requestedStrategy ? `，请求策略 ${run.requestedStrategy}` : ''}
            {run.effectiveStrategy ? `，实际策略 ${run.effectiveStrategy}` : ''}
          </Typography.Text>
        ) : null}
        {run.degraded ? <Typography.Text type="warning">检索降级：{run.degradationReason}</Typography.Text> : null}
        {run.answer ? (
          <div>
            <Typography.Text strong>回答</Typography.Text>
            <Typography.Paragraph style={{ marginBottom: 0 }}>{run.answer}</Typography.Paragraph>
          </div>
        ) : null}
        {run.sources.length > 0 ? <SourceList sources={run.sources} /> : null}
        {run.error ? <Typography.Text type="danger">{run.error}</Typography.Text> : null}
        {run.requestId ? <Typography.Text type="secondary">Request ID: {run.requestId}</Typography.Text> : null}
      </Space>
    </Card>
  );
}

function SourceList({ sources }: { sources: Citation[] }) {
  const visible = sources.filter((source) => source.content.trim() !== '');
  if (visible.length === 0) {
    return null;
  }
  return (
    <List
      size="small"
      header="引用来源"
      dataSource={visible}
      renderItem={(source) => (
        <List.Item>
          <Typography.Text>{source.content}</Typography.Text>
        </List.Item>
      )}
    />
  );
}

function StatusTag({ run, active }: { run: RunView; active: boolean }) {
  if (run.stopped) {
    return <Tag>已停止</Tag>;
  }
  if (run.error) {
    return <Tag color="error">失败</Tag>;
  }
  if (run.running || (active && !run.completed)) {
    return <Tag color="processing">执行中</Tag>;
  }
  if (run.completed) {
    return <Tag color="success">已完成</Tag>;
  }
  return <Tag>等待</Tag>;
}
