import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "UI_PROXY_");

  return {
    plugins: [react()],
    server: {
      host: "127.0.0.1",
      port: 5174,
      strictPort: true,
      allowedHosts: [
        "magma-dropout-bullpen.ngrok-free.dev",
      ],
      proxy: {
        "/ui-api": {
          target: env.UI_PROXY_TARGET || "http://127.0.0.1:8001",
          changeOrigin: false,
        },
      },
    },
  };
});