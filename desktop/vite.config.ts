import { defineConfig } from "vitest/config";

// The address and port are the ones `devUrl` in src-tauri/tauri.conf.json names. The dev server
// listens on this computer only.
export default defineConfig({
  clearScreen: false,
  server: {
    host: "127.0.0.1",
    port: 1420,
    strictPort: true,
    watch: { ignored: ["**/src-tauri/**"] },
  },
  build: {
    target: "es2022",
    sourcemap: false,
    // Everything the page loads is a file of the app: nothing is inlined into the HTML, so the
    // content security policy needs no hashes and no 'unsafe-inline'.
    assetsInlineLimit: 0,
    modulePreload: { polyfill: false },
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
  },
});
