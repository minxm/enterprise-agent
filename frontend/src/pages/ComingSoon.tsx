import { Result } from 'antd';

interface ComingSoonProps {
  name: string;
}

export default function ComingSoon({ name }: ComingSoonProps) {
  return <Result status="info" title="功能开发中" subTitle={`${name}尚未开发，当前没有业务数据。`} />;
}
