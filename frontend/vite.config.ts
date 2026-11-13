import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const api = process.env.GOLD_API_URL ?? "http://127.0.0.1:8742";

export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy: { "/api": api } },
});
