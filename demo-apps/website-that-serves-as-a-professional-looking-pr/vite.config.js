import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// The app is deployed under the /supernintendo-chaLLMers/ subpath
// (repo demo-apps convention). The Vite dev server serves the app at
// this base and provides SPA fallback, so deep links and refreshes
// resolve client-side in dev.
export default defineConfig({
  base: '/supernintendo-chaLLMers/',
  plugins: [react()],
});
