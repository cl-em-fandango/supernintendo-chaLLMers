# Hannah Montana Linux — product site

A single-page React app presenting the fictional, satirical operating system
**Hannah Montana Linux** ("Best of Both Worlds. Best of Both Kernels.") as a
polished commercial product page. Static content only: no backend, no network
calls at runtime, no real downloads.

## Install / run / build

```bash
npm install     # install dependencies (pinned exact versions)
npm run dev     # dev server at http://localhost:5173/supernintendo-chaLLMers/
npm run build   # production build into dist/
npm run preview # serve the production build locally
```

After `npm install` the app runs fully offline — no CDN assets, and the font
stacks are web-safe only.

## Structure

```
index.html            Vite entry
src/main.jsx          ReactDOM root + BrowserRouter (basename = BASE_URL)
src/App.jsx           Route table (5 routes + fallback)
src/navigation.js     Single source of truth for nav destinations
src/components/       Layout, Header, Footer, and per-view UI components
src/pages/            Home, Features, Testimonials, Pricing, GetHML
src/content.json      Baked-in generated content (data only, rendered as prose)
src/*Content.js       Per-view copy/data modules (strings only)
src/theme.css         Design tokens (CSS variables), shared chrome, responsive rules
src/<view>.css        Per-view styles
```

## Routing and deep links

- Router: React Router v6 `BrowserRouter` with a declarative route table:
  `/`, `/features`, `/testimonials`, `/pricing`, `/get` (unknown paths render
  Home).
- The app is deployed under the `/supernintendo-chaLLMers/` subpath
  (repo demo-apps convention). `vite.config.js` sets `base` and `main.jsx`
  passes `import.meta.env.BASE_URL` as the router `basename`, so links and
  deep links stay correct under the subpath.
- In development, the Vite dev server provides SPA fallback: opening or
  refreshing `…/supernintendo-chaLLMers/testimonials` directly resolves to
  `index.html` and renders client-side.

## Static hosting / SPA fallback

Because routes are client-side, any static host must serve `dist/index.html`
for unknown paths under the base path (SPA fallback), otherwise deep links and
refreshes on `/features` etc. will 404. Examples:

- Netlify: `_redirects` with `/supernintendo-chaLLMers/* /supernintendo-chaLLMers/index.html 200`
- nginx: `try_files $uri $uri/ /supernintendo-chaLLMers/index.html;`
- GitHub Pages: copy `dist/index.html` to `dist/404.html`

## Notes

- All copy is original parody; the three real-person testimonials on
  `/testimonials` are reproduced verbatim per spec and no other quotes are
  attributed to real people.
- The "download" button on `/get` shows an in-page toast and downloads
  nothing; the install snippet is fictional and inert.
- No copyrighted media: all graphics are CSS, gradients, and emoji
  (decorative ones marked `aria-hidden`).
- `node_modules/` and `dist/` are gitignored.
