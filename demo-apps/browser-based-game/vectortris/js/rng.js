/* rng.js — 7-bag tetromino randomizer (FR-03 / EC-07).
 *
 * Each bag holds all 7 piece types in shuffled order; a new bag is filled
 * only when the queue runs dry, so no type can appear three times before
 * another has appeared twice.
 */
(function (global) {
  'use strict';

  /* The 7 standard tetromino types (FR-02). */
  var PIECE_TYPES = Object.freeze(['I', 'O', 'T', 'S', 'Z', 'J', 'L']);

  /* SevenBag: a queue of upcoming piece types drawn bag by bag.
   * `randomFn` returns a float in [0, 1) and is injected so tests can be
   * deterministic. */
  function SevenBag(randomFn) {
    this.random = randomFn || Math.random;
    this.queue = [];
  }

  /* Fisher-Yates shuffle of a fresh bag appended to the queue. */
  SevenBag.prototype.fillBag = function () {
    var bag = PIECE_TYPES.slice();
    var i;
    var j;
    var swap;
    for (i = bag.length - 1; i > 0; i -= 1) {
      j = Math.floor(this.random() * (i + 1));
      swap = bag[i];
      bag[i] = bag[j];
      bag[j] = swap;
    }
    this.queue = this.queue.concat(bag);
  };

  SevenBag.prototype.ensure = function (count) {
    while (this.queue.length < count) {
      this.fillBag();
    }
  };

  /* Consume and return the next piece type. */
  SevenBag.prototype.next = function () {
    this.ensure(1);
    return this.queue.shift();
  };

  /* Look ahead at the next `count` types without consuming them. */
  SevenBag.prototype.upcoming = function (count) {
    this.ensure(count);
    return this.queue.slice(0, count);
  };

  global.VT.Rng = Object.freeze({
    PIECE_TYPES: PIECE_TYPES,
    SevenBag: SevenBag,
  });
})(typeof window !== 'undefined' ? window : globalThis);
