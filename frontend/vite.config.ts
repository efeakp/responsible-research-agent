import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development, /api is proxied to the FastAPI server (rra serve, port 8000).
// For a separate deployment, set VITE_API_URL to the API's origin at build time.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
  preview: {
    port: 4173,
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
});
