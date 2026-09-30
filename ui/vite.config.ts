import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The gateway serves the built app itself: Vite writes into src/engrai_server/static, and
// FastAPI mounts src/engrai_server/static/assets at /assets. Development uses Vite's build
// watcher too, keeping the UI and every API on the canonical gateway port.

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../src/engrai_server/static",
    emptyOutDir: true,
    // Safari 16 is the floor: it is the last version shipping on iPhone 8 and
    // the original iPad Pro, both of which are plausible wall displays.
    target: ["es2021", "safari16", "chrome108", "firefox115"],
    // The built bundle is committed so `pip install` works without Node, and a
    // 1.6 MB map that changes name on every build would dominate the history.
    // Build with ENGRAI_SOURCEMAP=1 when chasing a fault on a real device.
    sourcemap: process.env.ENGRAI_SOURCEMAP === "1",
  },
});
