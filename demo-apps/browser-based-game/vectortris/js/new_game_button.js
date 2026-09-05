/* new_game_button.js — the game-over screen's only affordance: the
 * "New Game" button. It appears with the "YOU SUCK" taunt, is reachable by
 * keyboard focus, and every activation (mouse click, or Enter/Space while it
 * has focus) navigates the same tab to REDIRECT_URL (FR-18).
 *
 * The button deliberately owns no game state and offers no restart path:
 * activation leaves the page, it never resumes or resets the game (FR-19,
 * AC-07, AC-11). The navigation target is injected through a Navigate
 * object so the rule is testable without a real browser location.
 */
(function (global) {
  'use strict';

  /* FR-18: the redirect destination. Same-tab navigation is preferred by
   * the spec so the redirect is unmistakable. */
  var REDIRECT_URL = 'https://meatspin.com';

  /* Keys that activate a focused control (FR-05 "Enter: confirm / activate
   * the focused control", FR-18 "Enter/Space"). */
  var ACTIVATION_KEYS = Object.freeze({ Enter: true, ' ': true });

  /* Navigate: the edge that actually leaves the page. Subclass or replace
   * `go` in tests to record the navigation instead. */
  function Navigate() {}

  Navigate.prototype.go = function (url) {
    if (global.location) {
      global.location.href = url;
    }
  };

  /* NewGameButtonOptions: explicit parameters object.
   * `navigate` is the Navigate used to leave the page. */
  function NewGameButtonOptions(navigate) {
    this.navigate = navigate || new Navigate();
  }

  /* NewGameButton: state + rules for the button, DOM-free.
   * `isVisible` mirrors whether the taunt screen is up; `activate` only
   * ever leaves the page and only once the game is over. */
  function NewGameButton(options) {
    var params = options || new NewGameButtonOptions();
    this.navigate = params.navigate;
    this.isVisible = false;
  }

  /* Keep the button in step with the taunt screen (FR-18: shown with or
   * after the taunt) and focus it as it appears, so Enter/Space reach it
   * without the mouse. Returns true when the visibility changed. */
  NewGameButton.prototype.update = function (game, element) {
    var shouldShow = typeof game.showsTaunt === 'function' && game.showsTaunt();
    if (shouldShow === this.isVisible) {
      return false;
    }
    this.isVisible = shouldShow;
    if (element) {
      element.hidden = !shouldShow;
      if (shouldShow && typeof element.focus === 'function') {
        element.focus();
      }
    }
    return true;
  };

  /* FR-18: activate the button. It only answers while the taunt screen is
   * up; before that it is hidden and inert, and nothing here can start or
   * restart a game (FR-19). Returns true when the page is leaving. */
  NewGameButton.prototype.activate = function (game) {
    if (
      !this.isVisible ||
      !game ||
      game.phase !== global.VT.Game.Phase.GAME_OVER
    ) {
      return false;
    }
    this.navigate.go(REDIRECT_URL);
    return true;
  };

  /* Wire a NewGameButton to a real <button> element.
   * - click: the mouse path and the browser's own Enter/Space activation.
   * - keydown on the button: Enter/Space are claimed here (preventDefault)
   *   so the browser does not also fire a second activation.
   * Returns the NewGameButton so the caller can keep it in sync per frame. */
  function attachNewGameButton(game, element, button) {
    var control = button || new NewGameButton(new NewGameButtonOptions());

    element.addEventListener('click', function () {
      control.activate(game);
    });

    element.addEventListener('keydown', function (event) {
      if (!ACTIVATION_KEYS[event.key]) {
        return;
      }
      event.preventDefault();
      control.activate(game);
    });

    control.update(game, element);
    return control;
  }

  global.VT.NewGameButton = Object.freeze({
    REDIRECT_URL: REDIRECT_URL,
    ACTIVATION_KEYS: ACTIVATION_KEYS,
    Navigate: Navigate,
    NewGameButtonOptions: NewGameButtonOptions,
    NewGameButton: NewGameButton,
    attachNewGameButton: attachNewGameButton,
  });
})(typeof window !== 'undefined' ? window : globalThis);
