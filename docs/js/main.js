/* main.js — composition root: looks up DOM elements, builds the Game,
 * attaches the keyboard and the game-over "New Game" button, keeps the
 * canvases fitted to their CSS boxes, and runs the requestAnimationFrame
 * loop (NFR-03). No business logic here — game rules live in js/game.js,
 * the redirect rule in js/new_game_button.js, drawing in js/render.js.
 */
(function (VT) {
  'use strict';

  function boot() {
    var playfieldCanvas = document.getElementById('playfield');
    var nextPanelCanvas = document.getElementById('next-panel');
    var holdPanelCanvas = document.getElementById('hold-panel');

    var playfieldCtx = VT.Render.fitCanvas(
      playfieldCanvas,
      VT.Render.playfieldSize()
    );
    var nextPanelCtx = VT.Render.fitCanvas(
      nextPanelCanvas,
      VT.Render.PANEL_SIZE
    );
    var holdPanelCtx = VT.Render.fitCanvas(
      holdPanelCanvas,
      VT.Render.PANEL_SIZE
    );

    var tickLine = document.getElementById('debug-tick');
    var hudScore = document.getElementById('hud-score');
    var hudLevel = document.getElementById('hud-level');
    var hudLines = document.getElementById('hud-lines');
    var newGameButtonElement = document.getElementById('new-game');

    var game = new VT.Game.Game(new VT.Game.GameOptions());
    VT.Input.attachKeyboard(game);
    var newGameButton = VT.NewGameButton.attachNewGameButton(
      game,
      newGameButtonElement
    );

    /* EC-10: refit the canvases to their (possibly new) CSS boxes. Game
     * state lives in `game`, so nothing is lost by the rescale — the next
     * rAF frame simply repaints it at the new size. */
    window.addEventListener('resize', function () {
      VT.Render.fitCanvas(playfieldCanvas, VT.Render.playfieldSize());
      VT.Render.fitCanvas(nextPanelCanvas, VT.Render.PANEL_SIZE);
      VT.Render.fitCanvas(holdPanelCanvas, VT.Render.PANEL_SIZE);
    });

    var frameCount = 0;
    var lastTimestamp = null;

    function frame(timestamp) {
      frameCount += 1;

      if (lastTimestamp !== null) {
        game.update(timestamp - lastTimestamp);
      }
      lastTimestamp = timestamp;

      newGameButton.update(game, newGameButtonElement);

      VT.Render.drawPlayfield(playfieldCtx, game);
      VT.Render.drawNextPanel(nextPanelCtx, game);
      VT.Render.drawHoldPanel(holdPanelCtx, game);

      hudScore.textContent = String(game.score);
      hudLevel.textContent = String(game.level);
      hudLines.textContent = String(game.lines);

      /* Proof the rAF loop is alive + 7-bag queue readout (slice 3
       * "Done when": spawn order and next preview are verifiable). */
      tickLine.textContent =
        'frame ' + frameCount + ' | queue: ' + game.nextTypes(5).join(' ');

      window.requestAnimationFrame(frame);
    }

    window.requestAnimationFrame(frame);
  }

  document.addEventListener('DOMContentLoaded', boot);
})(window.VT);
