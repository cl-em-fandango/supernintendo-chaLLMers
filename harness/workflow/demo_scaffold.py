"""Deterministic scaffold writer for generated demo apps (FR-3, FR-4.3).

Given a `StackPlan` and a Slice 6 `SiteContent`, this module writes a
minimal, buildable single-page app project into
`<apps_dir>/<app_name>/`: a declared build command, the stack's standard
artifact directory, a Pages-safe base/public path (FR-7.5), and the
generated content baked in as a `content.json` module the app imports.
The generation session (a pi run) then fleshes the app out; whatever the
model does afterwards, the scaffold alone already satisfies "builds
successfully and shows the generated content".

Every write is confined to the app directory (FR-3.3): paths are resolved
and checked against the app root before anything touches the filesystem,
so neither an app name nor a module path can escape it. The content
payload is written verbatim as JSON data and never executed (FR-4.3).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .demo_stack import StackPlan, WebStack

# App names are single kebab-case path segments; anything else (separators,
# `..`, absolute paths) is rejected before a directory is created.
_APP_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]*")

# FR-3.2: the exact dark-theme shape the default stack must carry.
CRA_DARK_THEME_SNIPPET = "createTheme({ palette: { mode: 'dark' } })"


class ScaffoldPathError(ValueError):
    """A scaffold write tried to land outside the app directory."""


def validate_app_name(app_name: str) -> str:
    """Return `app_name` when it is a safe single directory segment."""
    if not _APP_NAME_RE.fullmatch(str(app_name or "")):
        raise ScaffoldPathError(f"unsafe app name: {app_name!r}")
    return str(app_name)


def _write(app_root: Path, relative: str, text: str) -> Path:
    """Write `text` to `relative` under `app_root`, confined (FR-3.3)."""
    target = (app_root / relative).resolve()
    root = app_root.resolve()
    if target != root and root not in target.parents:
        raise ScaffoldPathError(
            f"scaffold write escapes the app directory: {relative!r}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def write_content_module(app_root: Path, plan: StackPlan, content) -> Path:
    """Bake the Slice 6 content document in as `content.json` (FR-4.3).

    CRA/Vue apps import it from `src/`; the no-build static app fetches
    it from the app root. The payload is serialized JSON data only.
    """
    relative = ("content.json" if plan.stack is WebStack.PLAIN_HTML
                else "src/content.json")
    return _write(app_root, relative, content.to_json() + "\n")


def scaffold_app(apps_dir: Path, app_name: str, plan: StackPlan,
                 content) -> Path:
    """Write the stack's project skeleton; return the app directory."""
    app_root = Path(apps_dir) / validate_app_name(app_name)
    app_root.mkdir(parents=True, exist_ok=True)
    write_content_module(app_root, plan, content)
    if plan.stack is WebStack.PLAIN_HTML:
        _scaffold_plain(app_root)
    elif plan.stack is WebStack.VUE:
        _scaffold_vue(app_root, plan)
    else:
        _scaffold_cra(app_root, plan)
    return app_root


# --- create-react-app + Material UI (the FR-3.2 default) -----------------

def _scaffold_cra(app_root: Path, plan: StackPlan) -> None:
    _write(app_root, "package.json", json.dumps({
        "name": app_root.name,
        "version": "0.1.0",
        "private": True,
        "homepage": plan.public_path,
        "dependencies": {
            "react": "^18.3.1",
            "react-scripts": "^5.0.1",
            "react-dom": "^18.3.1",
            "@mui/material": "^5.15.0",
            "@emotion/react": "^11.11.0",
            "@emotion/styled": "^11.11.0",
        },
        "scripts": {"start": "react-scripts start",
                    "build": "react-scripts build"},
        "browserslist": [">0.2%", "not dead"],
    }, indent=2) + "\n")
    _write(app_root, "public/index.html",
           '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
           '<meta charset="utf-8">\n'
           '<meta name="viewport" content="width=device-width, '
           'initial-scale=1">\n<title>demo app</title>\n</head>\n'
           "<body>\n<noscript>This app needs JavaScript.</noscript>\n"
           '<div id="root"></div>\n</body>\n</html>\n')
    _write(app_root, "src/index.js",
           "import React from 'react';\n"
           "import ReactDOM from 'react-dom/client';\n"
           "import App from './App';\n\n"
           "const root = ReactDOM.createRoot("
           "document.getElementById('root'));\n"
           "root.render(<App />);\n")
    _write(app_root, "src/App.js",
           "import React from 'react';\n"
           "import { ThemeProvider, createTheme } from "
           "'@mui/material/styles';\n"
           "import CssBaseline from '@mui/material/CssBaseline';\n"
           "import Container from '@mui/material/Container';\n"
           "import Typography from '@mui/material/Typography';\n"
           "import content from './content.json';\n\n"
           f"const darkTheme = {CRA_DARK_THEME_SNIPPET};\n\n"
           "function App() {\n"
           "  return (\n"
           "    <ThemeProvider theme={darkTheme}>\n"
           "      <CssBaseline />\n"
           '      <Container maxWidth="sm">\n'
           "        <Typography variant=\"h3\">{content.title}</"
           "Typography>\n"
           "        {(content.sections || []).map((section, index) => (\n"
           "          <section key={index}>\n"
           "            <Typography variant=\"h5\">"
           "{section.heading}</Typography>\n"
           "            <Typography variant=\"body1\">"
           "{section.body}</Typography>\n"
           "          </section>\n"
           "        ))}\n"
           "      </Container>\n"
           "    </ThemeProvider>\n"
           "  );\n"
           "}\n\n"
           "export default App;\n")


# --- Vue ------------------------------------------------------------------

def _scaffold_vue(app_root: Path, plan: StackPlan) -> None:
    _write(app_root, "package.json", json.dumps({
        "name": app_root.name,
        "version": "0.1.0",
        "private": True,
        "dependencies": {"vue": "^3.4.0"},
        "devDependencies": {"@vitejs/plugin-vue": "^5.0.0",
                            "vite": "^5.0.0"},
        "scripts": {"dev": "vite", "build": "vite build"},
    }, indent=2) + "\n")
    _write(app_root, "vite.config.js",
           "import { defineConfig } from 'vite';\n"
           "import vue from '@vitejs/plugin-vue';\n\n"
           "export default defineConfig({\n"
           f"  base: '{plan.public_path}',\n"
           "  plugins: [vue()],\n"
           "});\n")
    _write(app_root, "index.html",
           '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
           '<meta charset="utf-8">\n<title>demo app</title>\n</head>\n'
           "<body>\n<div id=\"app\"></div>\n"
           "<script type=\"module\" src=\"/src/main.js\"></script>\n"
           "</body>\n</html>\n")
    _write(app_root, "src/main.js",
           "import { createApp } from 'vue';\n"
           "import App from './App.vue';\n\n"
           "createApp(App).mount('#app');\n")
    _write(app_root, "src/App.vue",
           "<script setup>\n"
           "import content from './content.json';\n"
           "</script>\n\n"
           "<template>\n"
           "  <main>\n"
           "    <h1>{{ content.title }}</h1>\n"
           "    <section v-for=\"(section, index) in content.sections\"\n"
           "             :key=\"index\">\n"
           "      <h2>{{ section.heading }}</h2>\n"
           "      <p>{{ section.body }}</p>\n"
           "    </section>\n"
           "  </main>\n"
           "</template>\n")


# --- plain static HTML (no build) ------------------------------------------

def _scaffold_plain(app_root: Path) -> None:
    _write(app_root, "index.html",
           '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
           '<meta charset="utf-8">\n<title>demo app</title>\n</head>\n'
           "<body>\n<main id=\"app\"><h1>Loading…</h1></main>\n"
           "<script>\n"
           "fetch('./content.json')\n"
           "  .then((response) => response.json())\n"
           "  .then((content) => {\n"
           "    const app = document.getElementById('app');\n"
           "    app.replaceChildren();\n"
           "    const heading = document.createElement('h1');\n"
           "    heading.textContent = content.title;\n"
           "    app.append(heading);\n"
           "    (content.sections || []).forEach((section) => {\n"
           "      const node = document.createElement('section');\n"
           "      const title = document.createElement('h2');\n"
           "      title.textContent = section.heading;\n"
           "      const body = document.createElement('p');\n"
           "      body.textContent = section.body;\n"
           "      node.append(title, body);\n"
           "      app.append(node);\n"
           "    });\n"
           "  });\n"
           "</script>\n</body>\n</html>\n")
