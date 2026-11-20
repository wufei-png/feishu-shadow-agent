import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    // Keep the width queries readable by WebKit versions before Safari 16.4.
    cssTarget: "safari16",
    outDir: "../../src/feishu_shadow_agent/console_static",
    emptyOutDir: true
  }
});
