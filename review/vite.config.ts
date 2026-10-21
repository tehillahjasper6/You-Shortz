import react from "@vitejs/plugin-react";
import path from "node:path";
import { defineConfig } from "vite";
import { ROOT, ensureWorkspacePublic } from "../scripts/paths.ts";
import { reviewApi } from "./api.ts";

// The workspace's public/ is the static root, so the Player's staticFile("source.mp4")
// and the caption font resolve exactly as they do in a render.
export default defineConfig({
  root: path.join(ROOT, "review"),
  publicDir: ensureWorkspacePublic(),
  plugins: [react(), reviewApi()],
  server: { port: 5173, fs: { allow: [ROOT] } },
});
