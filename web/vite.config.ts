import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 开发态把后端接口代理到 FastAPI（127.0.0.1:8000），前端与后端同源，免 CORS。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/healthz": "http://127.0.0.1:8000",
      "/v1": "http://127.0.0.1:8000",
    },
  },
});
