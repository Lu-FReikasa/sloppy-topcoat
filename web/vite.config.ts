import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Dev server proxies the hub API (incl. the node WebSocket, so a node paired
// with the dev-server URL from the "add node" dialog can connect through it).
// Point HUB_URL at a running hub, e.g. HUB_URL=https://hub.example.lan:8443 npm run dev
const hubUrl = process.env.HUB_URL ?? "https://localhost:8443";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": {
        target: hubUrl,
        changeOrigin: true,
        ws: true,
        // The hub's certificate comes from its own CA (TLS mode "auto"); the dev proxy
        // runs on the developer's machine only, so it does not verify it.
        secure: false,
      },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}", "scripts/**/*.test.mjs"],
  },
});
