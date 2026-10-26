import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// Dev server on 5173 (falls back to the next free port if busy); /api is proxied to the FastAPI backend.
// TEE_API_URL (environment or frontend/.env) overrides the backend address; run.ps1 sets it when the
// API runs on a port other than 8000. Only TEE_* server-side settings are read here; nothing is
// exposed to browser code (that would need the VITE_ prefix).
export default defineConfig(({ mode }) => {
  const target = loadEnv(mode, ".", "TEE_").TEE_API_URL || "http://127.0.0.1:8000";
  const proxy = { "/api": { target, changeOrigin: true } };
  return {
    plugins: [react()],
    server: { port: 5173, strictPort: false, proxy },
    preview: { port: 4173, proxy },
  };
});
