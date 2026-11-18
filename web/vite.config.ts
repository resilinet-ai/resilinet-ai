/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Development: `npm run dev` serves the UI and forwards the API to `rai serve` on this
// machine. Production: Caddy serves dist/ and the API from one origin (deploy/caddy).
declare const process: { env: Record<string, string | undefined> }; // Node, without @types/node
const api = process.env.RAI_API_URL ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    proxy: { "/v1": api, "/healthz": api },
  },
  build: {
    sourcemap: false,
    // One origin, no inline scripts: the Content-Security-Policy stays strict.
    modulePreload: { polyfill: false },
    assetsInlineLimit: 0,
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["src/test-setup.ts"],
  },
});
