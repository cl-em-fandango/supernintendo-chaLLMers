# Vectortris

Open `index.html` directly (`file://`) — no build step, no dependencies, no
network access. Tests: `node tests/run_tests.js`.

## Files

| File | Responsibility |
| --- | --- |
| `index.html` | Page structure, HUD, canvas, control legend, New Game button |
| `style.css` | Layout and theme; viewport fitting (no scrolling) |
| `js/constants.js` | VT namespace, board dimensions, theme colours (data only) |
| `js/board.js` | Grid state: cells, occupancy, line clearing |
| `js/pieces.js` | Tetromino shapes and spawn positions |
| `js/rotation.js` | SRS rotation states and kick tables |
| `js/rng.js` | 7-bag randomizer |
| `js/explosion.js` | Game-over particle system |
| `js/game.js` | Game rules and phases (state machine, scoring, gravity) |
| `js/new_game_button.js` | Game-over "New Game" affordance and its redirect rule |
| `js/render.js` | Canvas drawing and CSS-box fitting (no state) |
| `js/input.js` | Keyboard mapping, DAS repeat, pause/blur input freeze |
| `js/main.js` | Composition root: DOM lookup, resize wiring, rAF loop |

## Acceptance sweep (spec section 8)

Logic-level criteria are covered by `tests/run_tests.js` (211 checks);
browser-only criteria were checked by hand against the page.

- **AC-01** — Pass. `index.html` loads over `file://` with no console errors;
  the READY screen starts the game on the first keypress (`game.start()` from
  `js/input.js`, refused once the game is over).
- **AC-02** — Pass. All FR-05 controls are bound in `js/input.js`: movement,
  soft/hard drop, both rotations with SRS kicks (`js/rotation.js`, EC-02),
  hold (C/Shift), pause (P/Esc), restart keys. Tests cover kicks, hold and drops.
- **AC-03** — Pass. Level gravity curve (`gravityIntervalMs`) speeds up every
  10 lines; scoring and the Score/Level/Lines HUD follow FR-11/FR-12 (tests).
- **AC-04** — Pass. Ghost row is derived from the live piece and board each
  frame; the Next preview is read off the bag queue, so it always matches the
  piece that spawns (tests: "next preview always matches the piece that spawns",
  "ghost row equals the real landing row").
- **AC-05** — Pass. Block-out snapshots the whole board into the explosion and
  empties the grid; "YOU SUCK" is drawn from a quarter into the blast and kept
  (tests: "the whole board is consumed by the end of the explosion").
- **AC-06** — Pass. `js/new_game_button.js` shows the focusable
  `#new-game` button with the taunt, focuses it, and navigates the same tab to
  the meatspin redirect target on click and on Enter/Space (tests: "AC-06: clicking
  New Game navigates to meatspin.com", Enter/Space activation).
- **AC-07** — Pass. Every gameplay action requires `Phase.FALLING`, and
  `start()` stays refused after game over (tests: "no restart after game over
  (FR-19)", "AC-07/AC-11: activation leaves the game over, it never restarts").
- **AC-08** — Pass. The deliverable is HTML/CSS/JS only; the AC-08 test walks
  the tree and rejects asset files, CSS resource references, external font
  declarations and imports, media and frame elements, browser network APIs,
  and any URL other than the redirect.
- **AC-09** — Pass. `html, body { overflow: hidden }` and every bound key is
  `preventDefault()`ed while playing, so arrows and Space never scroll.
- **AC-10** — Pass. Pause freezes gravity and swallows gameplay input except
  the pause key; blur auto-pauses and drops held inputs, and ticks are clamped
  so there is no jump on resume (tests: EC-03, EC-08).
- **AC-11** — Pass. The only post-game-over affordance is the redirecting
  New Game button (or a manual page reload); Enter/Space are handed to the
  focused button and every other key is inert (tests: FR-18 handoff, EC-04).

## Edge cases

- **EC-10** — `fitCanvas()` maps a fixed logical drawing space (300x600
  playfield, 120x80 panels) onto the canvas' current CSS box at
  `devicePixelRatio`. `js/main.js` refits on boot and on every `resize`; game
  state lives in the `Game`, so a rescale repaints it unchanged.
