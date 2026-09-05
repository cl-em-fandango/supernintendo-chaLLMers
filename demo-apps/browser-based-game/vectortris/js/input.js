/* input.js — keyboard handler: maps FR-05 keys to game actions, applies
 * DAS-style auto-repeat to movement/rotation while held (FR-06), consumes
 * arrows/space so the page never scrolls (FR-08 / AC-09), freezes all
 * gameplay keys while paused except the pause toggle (EC-03), and
 * auto-pauses when the window loses focus (EC-08). Pausing and focus loss
 * also drop every held key, so the game resumes cleanly instead of
 * repeating a key whose keyup never arrived (AC-10).
 *
 * The DAS timing logic is the pure KeyRepeater class (press / release /
 * releaseAll / tick); attachKeyboard only wires DOM events to it and to
 * the Game.
 */
(function (global) {
  'use strict';

  /* keydown key value -> Game method name. */
  var KEY_ACTIONS = Object.freeze({
    ArrowLeft: 'moveLeft',
    ArrowRight: 'moveRight',
    ArrowDown: 'softDrop',
    ArrowUp: 'rotateCW',
    x: 'rotateCW',
    X: 'rotateCW',
    z: 'rotateCCW',
    Z: 'rotateCCW',
    Control: 'rotateCCW',
    ' ': 'hardDrop',
    c: 'hold',
    C: 'hold',
    Shift: 'hold',
    p: 'togglePause',
    P: 'togglePause',
    Escape: 'togglePause',
    Backspace: 'start',
    r: 'start',
    R: 'start',
  });

  /* FR-06: actions that auto-repeat while held. Soft drop is excluded —
   * holding Down uses accelerated gravity instead (FR-05). */
  var REPEATABLE_ACTIONS = Object.freeze({
    moveLeft: true,
    moveRight: true,
    rotateCW: true,
    rotateCCW: true,
  });

  /* Delay before a held key starts repeating, interval between repeats,
   * and how often the repeat timer fires. */
  var DAS_DELAY_MS = 170;
  var ARR_INTERVAL_MS = 40;
  var REPEAT_TICK_MS = 16;

  /* RepeaterOptions: explicit parameters object for KeyRepeater. */
  function RepeaterOptions(dasDelayMs, arrIntervalMs) {
    this.dasDelayMs = dasDelayMs || DAS_DELAY_MS;
    this.arrIntervalMs = arrIntervalMs || ARR_INTERVAL_MS;
  }

  /* KeyRepeater: pure DAS state machine. press(action) arms the held key,
   * release(action) disarms it, tick(elapsedMs) returns how many repeat
   * firings have come due. attachKeyboard drives it from a timer; tests
   * drive tick() directly. */
  function KeyRepeater(options) {
    var params = options || new RepeaterOptions();
    this.dasDelayMs = params.dasDelayMs;
    this.arrIntervalMs = params.arrIntervalMs;
    this.heldAction = null;
    this.heldElapsedMs = 0;
    this.repeatElapsedMs = 0;
  }

  KeyRepeater.prototype.press = function (action) {
    this.heldAction = action;
    this.heldElapsedMs = 0;
    this.repeatElapsedMs = 0;
  };

  KeyRepeater.prototype.release = function (action) {
    if (this.heldAction === action) {
      this.releaseAll();
    }
  };

  /* Drop the held key whatever it is — used when keyup events stop
   * arriving (focus loss, pause). */
  KeyRepeater.prototype.releaseAll = function () {
    this.heldAction = null;
    this.heldElapsedMs = 0;
    this.repeatElapsedMs = 0;
  };

  KeyRepeater.prototype.tick = function (elapsedMs) {
    if (this.heldAction === null) {
      return 0;
    }
    this.heldElapsedMs += elapsedMs;
    if (this.heldElapsedMs < this.dasDelayMs) {
      return 0;
    }
    this.repeatElapsedMs += elapsedMs;
    var firings = 0;
    while (this.repeatElapsedMs >= this.arrIntervalMs) {
      this.repeatElapsedMs -= this.arrIntervalMs;
      firings += 1;
    }
    return firings;
  };

  /* Attach the keydown/keyup/blur listeners that drive `game`.
   * `target` defaults to window; `timers` supplies setInterval and
   * defaults to the global scope (tests inject a fake or omit it).
   * preventDefault keeps the page from scrolling while playing
   * (FR-08 / AC-09). On the READY start screen any keypress starts a
   * fresh game (AC-01/AC-11); the key is consumed, not replayed as
   * gameplay. */
  function attachKeyboard(game, target, timers) {
    var element = target || global.window;
    var clock = timers || global;
    var repeater = new KeyRepeater(new RepeaterOptions());
    var repeatTimerId = null;

    function stopRepeatTimer() {
      if (repeatTimerId !== null && typeof clock.clearInterval === 'function') {
        clock.clearInterval(repeatTimerId);
      }
      repeatTimerId = null;
    }

    /* Pause and focus loss swallow the keyup that ends a held key — the
     * browser never delivers it. Drop every held input so resuming does
     * not leave the piece sliding or soft-dropping on its own
     * (AC-10 / EC-08). */
    function dropHeldInputs() {
      repeater.releaseAll();
      stopRepeatTimer();
      if (typeof game.setSoftDropHeld === 'function') {
        game.setSoftDropHeld(false);
      }
    }

    function runAction(actionName) {
      /* EC-03: while paused every gameplay key is ignored; only the
       * pause toggle passes. */
      if (game.paused && actionName !== 'togglePause') {
        return;
      }
      game[actionName]();
    }

    function repeatTick() {
      var firings = repeater.tick(REPEAT_TICK_MS);
      if (repeater.heldAction === null) {
        stopRepeatTimer();
        return;
      }
      for (var i = 0; i < firings; i += 1) {
        runAction(repeater.heldAction);
      }
    }

    element.addEventListener('keydown', function (event) {
      if (game.phase === global.VT.Game.Phase.READY) {
        event.preventDefault();
        game.start();
        return;
      }
      /* FR-18/EC-04: on the game-over screen Enter and Space belong to the
       * focused "New Game" button, not to gameplay. They are left
       * unconsumed so the button's own handler (and the browser's native
       * activation) gets them; every other key stays swallowed and inert. */
      if (
        game.phase === global.VT.Game.Phase.GAME_OVER &&
        (event.key === 'Enter' || event.key === ' ')
      ) {
        return;
      }
      var actionName = KEY_ACTIONS[event.key];
      if (!actionName) {
        return;
      }
      event.preventDefault();
      if (actionName === 'togglePause') {
        game.togglePause();
        if (game.paused) {
          dropHeldInputs();
        }
        return;
      }
      if (game.paused) {
        return;
      }
      if (event.repeat) {
        /* OS auto-repeat: the DAS timer owns repeats (FR-06). */
        return;
      }
      if (actionName === 'softDrop') {
        /* FR-05: 1 cell per press; accelerated gravity while held. */
        game.softDrop();
        game.setSoftDropHeld(true);
        return;
      }
      runAction(actionName);
      if (REPEATABLE_ACTIONS[actionName]) {
        repeater.press(actionName);
        if (repeatTimerId === null && typeof clock.setInterval === 'function') {
          repeatTimerId = clock.setInterval(repeatTick, REPEAT_TICK_MS);
        }
      }
    });

    element.addEventListener('keyup', function (event) {
      var actionName = KEY_ACTIONS[event.key];
      if (!actionName) {
        return;
      }
      if (actionName === 'softDrop') {
        /* FR-11: the soft-drop descent accumulator resets on release. */
        game.setSoftDropHeld(false);
      }
      repeater.release(actionName);
      if (repeater.heldAction === null) {
        stopRepeatTimer();
      }
    });

    /* EC-08: losing focus auto-pauses and drops every held key; update()
     * clamps its elapsed time, so returning to the tab neither jump-drops
     * the piece nor resumes a phantom key repeat. */
    element.addEventListener('blur', function () {
      dropHeldInputs();
      if (typeof game.pause === 'function') {
        game.pause();
      }
    });
  }

  global.VT.Input = Object.freeze({
    KEY_ACTIONS: KEY_ACTIONS,
    REPEATABLE_ACTIONS: REPEATABLE_ACTIONS,
    DAS_DELAY_MS: DAS_DELAY_MS,
    ARR_INTERVAL_MS: ARR_INTERVAL_MS,
    REPEAT_TICK_MS: REPEAT_TICK_MS,
    RepeaterOptions: RepeaterOptions,
    KeyRepeater: KeyRepeater,
    attachKeyboard: attachKeyboard,
  });
})(typeof window !== 'undefined' ? window : globalThis);
