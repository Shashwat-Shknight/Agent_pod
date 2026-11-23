import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Development: `npm run dev` serves the UI on :5173 and forwards API calls to FastAPI on :8000,
// so the browser always talks to one origin (same cookies, no CORS).
// Production: `npm run build` writes web/dist, which FastAPI serves on :8000 - no Node needed at runtime.
const backend = "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": { target: backend, changeOrigin: false },
      "/health": { target: backend },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
    chunkSizeWarningLimit: 900,
  },
  test: {
    environment: "jsdom",
    globals: false,
  },
});
