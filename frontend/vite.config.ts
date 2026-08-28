/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import http from "http";

export default defineConfig({
  plugins: [react()],
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["./src/test-setup.ts"],
    css: true,
  },
  server: {
    host: "0.0.0.0",
    port: 5173,
    proxy: {
      "/api": {
        target: "http://backend:8080",
        changeOrigin: true,
        // Disable DNS caching by using a custom agent
        agent: new http.Agent({ keepAlive: false, maxSockets: 1 }),
      },
    },
    // Force polling for file changes — required for Docker Desktop on Windows
    // where OS-level file events don't propagate through bind mounts.
    watch: {
      usePolling: true,
    },
  },
});
