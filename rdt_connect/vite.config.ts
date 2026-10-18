import { defineConfig } from "vite";

export default defineConfig({
  build: { rollupOptions: { input: { main: "index.html", updates: "update.html" } } },
  clearScreen: false,
  server: {
    host: "127.0.0.1",
    port: 1420,
    strictPort: true,
  },
});
