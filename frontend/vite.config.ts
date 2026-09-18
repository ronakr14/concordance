import { fileURLToPath, URL } from "node:url";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The browser talks to the API through `/api` on its own origin - in
// development through this proxy, in the container through nginx. Same origin
// means no CORS preflight, and it is what lets the refresh cookie be
// SameSite=Strict and scoped to `/api/auth`.
const API_TARGET = process.env.CONCORDANCE_API ?? "http://127.0.0.1:8000";

const proxy = {
  "/api": {
    target: API_TARGET,
    changeOrigin: true,
    rewrite: (path: string) => path.replace(/^\/api/, ""),
  },
};

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  server: { port: 5173, strictPort: true, proxy },
  // `vite preview` serves the production build with the same proxy, which is
  // how the build is proven not to depend on the dev server.
  preview: { port: 4173, strictPort: true, proxy },
  build: { sourcemap: true },
});
