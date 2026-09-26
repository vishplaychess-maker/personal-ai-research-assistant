/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { VitePWA } from "vite-plugin-pwa";
import http from "http";
import { resolveDevProxyTarget } from "./src/lib/devProxyTarget";

// MP-3C3D6B: Node-side, development-only proxy target. Unset in every normal run,
// in which case the original Docker-service default is returned unchanged. This
// value never reaches `define`/`import.meta.env`, so it cannot enter a bundle.
// `globalThis` is used instead of the bare `process` identifier because this file
// is type-checked by a Node-only tsconfig that has no `@types/node` installed.
const NODE_ENV = (globalThis as { process?: { env?: Record<string, string | undefined> } })
  .process?.env;
const DEV_PROXY_TARGET = resolveDevProxyTarget(NODE_ENV?.THUNDER_DEV_PROXY_TARGET);

export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      registerType: "autoUpdate",
      includeAssets: ["favicon.ico", "robots.txt"],
      manifest: {
        name: "Thunder AI",
        short_name: "Thunder AI",
        description: "Thunder AI — your AI research assistant with document analysis, web search, and memory.",
        theme_color: "#09090B",
        background_color: "#09090B",
        display: "standalone",
        orientation: "portrait-primary",
        scope: "/",
        start_url: "/",
        icons: [
          {
            src: "icons/icon-192.png",
            sizes: "192x192",
            type: "image/png",
            purpose: "any maskable"
          },
          {
            src: "icons/icon-512.png",
            sizes: "512x512",
            type: "image/png",
            purpose: "any maskable"
          }
        ]
      },
      workbox: {
        globPatterns: ["**/*.{js,css,html,ico,png,svg,woff2}"],
        cleanupOutdatedCaches: true,
        runtimeCaching: [
          {
            urlPattern: /^https:\/\/fonts\.googleapis\.com\/.*/i,
            handler: "CacheFirst",
            options: {
              cacheName: "google-fonts-cache",
              expiration: {
                maxEntries: 10,
                maxAgeSeconds: 60 * 60 * 24 * 365 // 1 year
              },
              cacheableResponse: {
                statuses: [0, 200]
              }
            }
          },
          {
            urlPattern: /^https:\/\/fonts\.gstatic\.com\/.*/i,
            handler: "CacheFirst",
            options: {
              cacheName: "gstatic-fonts-cache",
              expiration: {
                maxEntries: 10,
                maxAgeSeconds: 60 * 60 * 24 * 365
              },
              cacheableResponse: {
                statuses: [0, 200]
              }
            }
          }
        ]
      }
    })
  ],
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
        target: DEV_PROXY_TARGET,
        changeOrigin: true,
        // A dedicated, NON-REUSING agent - an SSE response must never be pooled
        // and reused - but with NO socket cap. `maxSockets: 1` let ONE open
        // durable stream (a run parked at `waiting_for_approval`) own the only
        // proxy socket, so every other /api request (approval detail, exact-code
        // preview, health) queued behind it indefinitely and the approval dialog
        // never loaded. Concurrency is bounded by the browser, not by a single
        // proxy socket.
        agent: new http.Agent({ keepAlive: false }),
      },
    },
    watch: {
      usePolling: true,
    },
  },
});