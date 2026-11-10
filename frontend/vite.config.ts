import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// 开发请求直接使用 VITE_API_BASE_URL。Java 目前没有 CORS，
// 这里不另挂 /api 代理，避免和绝对地址同时生效。
export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
  },
  preview: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
  },
});
