import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const javaTarget = 'http://127.0.0.1:8080';

// 浏览器只访问开发服务器。/api 与 /actuator 转发到 Java，不配置 Python 代理，也不启用 CORS。
export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': { target: javaTarget, changeOrigin: true },
      '/actuator': { target: javaTarget, changeOrigin: true },
    },
  },
  preview: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
  },
});
