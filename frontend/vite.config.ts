import { fileURLToPath, URL } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  server: {
    // Mobile-first: the PWA is developed on a phone on the LAN as much as on
    // this machine, so bind all interfaces rather than localhost only.
    host: true,
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
      // Regex, not the prefix "/s" — Vite matches proxy keys by prefix, so a
      // plain "/s" also swallows "/src/main.tsx" and every other module, which
      // silently proxies the whole app source to the API and renders a blank
      // page. The tag-tap path itself cannot move: it is physically written
      // into every NFC tag and printed QR.
      "^/s/[A-Za-z0-9-]+$": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
  test: {
    globals: true,
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
