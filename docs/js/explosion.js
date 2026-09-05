/* explosion.js — game-over particle system (FR-16): on block-out the whole
 * board is snapshotted and every placed block shatters into shards that
 * flash, fly outward, sag under gravity and fade out over a few seconds.
 *
 * Pure simulation: it owns the shard state and its time evolution only.
 * Drawing lives in render.js, the phase machine in game.js. Coordinates
 * are CSS pixels in playfield space (x right, y down).
 */
(function (global) {
  'use strict';

  /* FR-16: the explosion consumes the board over roughly 2-5 s. */
  var DURATION_MS = 3000;

  /* Shards spawned per destroyed block. */
  var PARTICLES_PER_BLOCK = 6;

  /* Shard launch speed range, px per ms, and the downward pull applied
   * afterwards, px per ms^2. */
  var MIN_SPEED_PX_PER_MS = 0.03;
  var MAX_SPEED_PX_PER_MS = 0.35;
  var GRAVITY_PX_PER_MS2 = 0.0005;

  /* A shard burns white for this long right after shattering (FR-16
   * "brief flashes") before taking on its block's color. */
  var FLASH_MS = 120;

  /* Shard lifetime is a random fraction of the explosion duration within
   * these bounds, so blocks vanish progressively instead of all at once. */
  var MIN_LIFE_FRACTION = 0.5;
  var MAX_LIFE_FRACTION = 1.0;

  /* Shard side length range, px. */
  var MIN_SIZE_PX = 4;
  var MAX_SIZE_PX = 9;

  /* ExplosionOptions: explicit parameters object. `random` is an optional
   * [0,1) generator injected by tests. */
  function ExplosionOptions(durationMs, particlesPerBlock, random) {
    this.durationMs = durationMs || DURATION_MS;
    this.particlesPerBlock = particlesPerBlock || PARTICLES_PER_BLOCK;
    this.random = random || null;
  }

  /* Particle: one shard of a shattered block. Position starts at the
   * block's center; velocity, size and lifetime are fixed at spawn. */
  function Particle(x, y, vx, vy, sizePx, color, lifeMs) {
    this.x = x;
    this.y = y;
    this.vx = vx;
    this.vy = vy;
    this.sizePx = sizePx;
    this.color = color;
    this.elapsedMs = 0;
    this.lifeMs = lifeMs;
  }

  Particle.prototype.update = function (elapsedMs) {
    this.elapsedMs += elapsedMs;
    this.x += this.vx * elapsedMs;
    this.y += this.vy * elapsedMs;
    this.vy += GRAVITY_PX_PER_MS2 * elapsedMs;
  };

  Particle.prototype.isAlive = function () {
    return this.elapsedMs < this.lifeMs;
  };

  /* Opacity 0..1: full until 60% of the shard's life, then a linear fade
   * to nothing. */
  Particle.prototype.alpha = function () {
    var life = this.elapsedMs / this.lifeMs;
    if (life < 0.6) {
      return 1;
    }
    return Math.max(0, 1 - (life - 0.6) / 0.4);
  };

  Particle.prototype.isFlashing = function () {
    return this.elapsedMs < FLASH_MS;
  };

  /* Explosion: snapshot every occupied board cell as a burst of shards.
   * The board itself is not modified — game.js empties it after the
   * snapshot so the render loop draws only the debris. */
  function Explosion(board, options) {
    var params = options || new ExplosionOptions();
    this.durationMs = params.durationMs;
    this.particlesPerBlock = params.particlesPerBlock;
    this.randomFn = params.random || Math.random;
    this.elapsedMs = 0;
    this.blockCount = 0;
    this.particles = [];
    this.captureBoard(board);
  }

  Explosion.prototype.captureBoard = function (board) {
    var cell = global.VT.Board.CELL_SIZE_PX;
    var row;
    var col;
    for (row = 0; row < board.rows; row += 1) {
      for (col = 0; col < board.columns; col += 1) {
        var type = board.cells[row][col];
        if (type !== null) {
          this.blockCount += 1;
          this.spawnShards(
            col * cell + cell / 2,
            row * cell + cell / 2,
            global.VT.TetrominoColors[type]
          );
        }
      }
    }
  };

  /* One block's worth of shards: random direction (full circle, so pieces
   * fly outward from the board in every direction), random speed, size
   * and lifetime. */
  Explosion.prototype.spawnShards = function (x, y, color) {
    var i;
    for (i = 0; i < this.particlesPerBlock; i += 1) {
      var angle = this.randomFn() * Math.PI * 2;
      var speed =
        MIN_SPEED_PX_PER_MS +
        this.randomFn() * (MAX_SPEED_PX_PER_MS - MIN_SPEED_PX_PER_MS);
      var lifeFraction =
        MIN_LIFE_FRACTION +
        this.randomFn() * (MAX_LIFE_FRACTION - MIN_LIFE_FRACTION);
      var sizePx =
        MIN_SIZE_PX + this.randomFn() * (MAX_SIZE_PX - MIN_SIZE_PX);
      this.particles.push(
        new Particle(
          x,
          y,
          Math.cos(angle) * speed,
          Math.sin(angle) * speed,
          sizePx,
          color,
          this.durationMs * lifeFraction
        )
      );
    }
  };

  /* Advance the simulation and drop expired shards. Called every frame
   * from game.js while the game-over explosion runs (NFR-03). */
  Explosion.prototype.update = function (elapsedMs) {
    this.elapsedMs += elapsedMs;
    var survivors = [];
    var i;
    for (i = 0; i < this.particles.length; i += 1) {
      this.particles[i].update(elapsedMs);
      if (this.particles[i].isAlive()) {
        survivors.push(this.particles[i]);
      }
    }
    this.particles = survivors;
  };

  /* 0..1 elapsed fraction of the whole explosion. */
  Explosion.prototype.progress = function () {
    return Math.min(1, this.elapsedMs / this.durationMs);
  };

  /* The explosion is over once its duration has run — every shard's life
   * is at most durationMs, so the board is fully consumed. */
  Explosion.prototype.isComplete = function () {
    return this.elapsedMs >= this.durationMs;
  };

  global.VT.Explosion = Object.freeze({
    DURATION_MS: DURATION_MS,
    PARTICLES_PER_BLOCK: PARTICLES_PER_BLOCK,
    FLASH_MS: FLASH_MS,
    ExplosionOptions: ExplosionOptions,
    Particle: Particle,
    Explosion: Explosion,
  });
})(typeof window !== 'undefined' ? window : globalThis);
