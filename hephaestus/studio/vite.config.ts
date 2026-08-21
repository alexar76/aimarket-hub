import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath } from 'node:url';

export default defineConfig({
  plugins: [react()],
  // The hub mounts the built bundle at /studio, so every asset URL has to be written
  // relative to that prefix — an absolute /assets/... 404s behind the mount.
  base: '/studio/',
  resolve: {
    alias: { '@core': fileURLToPath(new URL('../src', import.meta.url)) },
  },
  build: { outDir: 'dist', emptyOutDir: true, sourcemap: false },
});
