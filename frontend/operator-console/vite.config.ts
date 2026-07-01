import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../../src/feishu_shadow_agent/console_static",
    emptyOutDir: true
  }
});
