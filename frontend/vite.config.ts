import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const api = process.env.GOLD_API_URL ?? "http://127.0.0.1:8742";

export default defineConfig({
  plugins: [react()],
  // Dev server on its own port (5173 is commonly used by other Vite projects); strictPort fails loudly instead of silently moving.
  server: { port: 5183, strictPort: true, proxy: { "/api": api } },
});
