/* mobile_controls.js — small touch UI layered on top of the existing game API. */
(function (global) {
  'use strict';

  var REPEAT_ACTIONS = { moveLeft: true, moveRight: true };

  function attachMobileControls(game, container, timers) {
    if (!container) {
      return;
    }
    var clock = timers || global;
    var repeatId = null;
    var activeButton = null;

    function stop(button) {
      if (repeatId !== null && typeof clock.clearInterval === 'function') {
        clock.clearInterval(repeatId);
      }
      repeatId = null;
      if (button && button.dataset.action === 'softDrop') {
        game.setSoftDropHeld(false);
      }
      activeButton = null;
    }

    function press(button, event) {
      var action = button.dataset.action;
      if (!action) {
        return;
      }
      if (game.phase === global.VT.Game.Phase.READY) {
        game.start();
        return;
      }
      if (game.paused && action !== 'togglePause') {
        return;
      }
      if (game.phase !== global.VT.Game.Phase.FALLING) {
        return;
      }
      if (button.setPointerCapture) {
        button.setPointerCapture(event.pointerId);
      }
      game[action]();
      if (action === 'softDrop') {
        game.setSoftDropHeld(true);
      } else if (REPEAT_ACTIONS[action] && typeof clock.setInterval === 'function') {
        repeatId = clock.setInterval(function () {
          if (!game.paused) {
            game[action]();
          }
        }, 100);
      }
      activeButton = button;
    }

    Array.prototype.forEach.call(container.querySelectorAll('button'), function (button) {
      button.addEventListener('pointerdown', function (event) {
        event.preventDefault();
        stop(activeButton);
        press(button, event);
      });
      button.addEventListener('pointerup', function (event) {
        event.preventDefault();
        stop(button);
      });
      button.addEventListener('pointercancel', function () { stop(button); });
      button.addEventListener('pointerleave', function (event) {
        if (event.buttons === 0) {
          stop(button);
        }
      });
      button.addEventListener('click', function (event) {
        event.preventDefault();
      });
    });
  }

  global.VT.MobileControls = Object.freeze({
    attachMobileControls: attachMobileControls,
  });
})(typeof window !== 'undefined' ? window : globalThis);
