/* game.js — the game state machine: phase, 7-bag spawn queue, gravity
 * timing with the level speed curve, player actions (move / soft drop /
 * hard drop), lock delay with a reset cap, drop and line-clear scoring,
 * pause and block-out game over.
 *
 * Pure logic: no DOM, no canvas. main.js drives update() from the rAF loop
 * and forwards keydown events from input.js to the action methods.
 */
(function (global) {
  'use strict';

  /* Discrete game phases — an enum object, not scattered strings.
   * READY: title/start screen (slice 3 starts on first keypress). */
  var Phase = Object.freeze({
    READY: 'READY',
    FALLING: 'FALLING',
    GAME_OVER: 'GAME_OVER',
  });

  /* Sub-state of GAME_OVER (slice 7): the board-consuming explosion
   * plays out (FR-16), then the "YOU SUCK" taunt screen remains — an
   * end state with no way back (FR-19). */
  var GameOverStage = Object.freeze({
    EXPLODING: 'EXPLODING',
    TAUNT: 'TAUNT',
  });

  /* FR-17/FR-18: how far the explosion must run before the taunt text and
   * the "New Game" button appear ("during/after the explosion") and then
   * stay for good. */
  var TAUNT_REVEAL_PROGRESS = 0.25;

  /* FR-09: level-1 gravity is ~1000 ms per row; each level multiplies the
   * interval by GRAVITY_LEVEL_DECAY so speed visibly increases (curve is
   * implementer's choice per spec). */
  var BASE_GRAVITY_MS = 1000;
  var GRAVITY_LEVEL_DECAY = 0.85;
  var MIN_GRAVITY_MS = 50;

  /* FR-12: one level per 10 lines cleared. */
  var LINES_PER_LEVEL = 10;

  /* How many upcoming types the game keeps visible for preview/debug. */
  var QUEUE_LOOKAHEAD = 5;

  /* FR-11 line-clear base scores; index = rows cleared at once. */
  var LINE_CLEAR_SCORES = Object.freeze([0, 100, 300, 500, 800]);

  /* FR-10: a piece that cannot move down locks after this much time on
   * the ground; each successful move/rotation restarts the countdown,
   * at most MAX_LOCK_RESETS times per piece. */
  var LOCK_DELAY_MS = 500;
  var MAX_LOCK_RESETS = 15;

  /* FR-11 drop bonuses, per cell descended. Not level-multiplied. */
  var SOFT_DROP_SCORE_PER_CELL = 1;
  var HARD_DROP_SCORE_PER_CELL = 2;

  /* FR-05: gravity interval while Down is held (accelerated fall). */
  var SOFT_DROP_GRAVITY_MS = 50;

  /* EC-08: one update() never advances more than this many ms, so a
   * backgrounded tab (huge rAF delta) cannot jump-drop the piece. */
  var MAX_TICK_MS = 100;

  /* FR-09 gravity interval for a level, in ms. */
  function gravityIntervalMs(level) {
    return Math.max(
      MIN_GRAVITY_MS,
      Math.round(BASE_GRAVITY_MS * Math.pow(GRAVITY_LEVEL_DECAY, level - 1))
    );
  }

  /* GameOptions: explicit parameters object for constructing a Game.
   * `random` is an optional [0,1) generator injected by tests. */
  function GameOptions(random) {
    this.random = random || null;
  }

  function Game(options) {
    var params = options || new GameOptions();
    this.randomFn = params.random || Math.random;
    this.reset();
  }

  /* Return to the pristine start-screen state: empty board, fresh bag. */
  Game.prototype.reset = function () {
    this.board = new global.VT.Board.GameBoard();
    this.phase = Phase.READY;
    this.currentPiece = null;
    this.score = 0;
    this.lines = 0;
    this.level = 1;
    this.gravityElapsedMs = 0;
    this.bag = new global.VT.Rng.SevenBag(this.randomFn);
    this.nextQueue = [];
    this.heldType = null;
    this.holdUsed = false;
    this.paused = false;
    this.lockElapsedMs = 0;
    this.lockResetsUsed = 0;
    this.softDropHeld = false;
    this.softDropDescentCells = 0;
    this.gameOverStage = null;
    this.explosion = null;
    this.refillQueue();
  };

  Game.prototype.refillQueue = function () {
    while (this.nextQueue.length < QUEUE_LOOKAHEAD) {
      this.nextQueue.push(this.bag.next());
    }
  };

  /* FR-05 (start screen): begin a fresh game from READY. Any other phase
   * refuses — mid-game restart is out of scope and post-game-over restart
   * is forbidden (FR-19). Returns true when the game started. */
  Game.prototype.start = function () {
    if (this.phase !== Phase.READY) {
      return false;
    }
    this.spawnNext();
    return true;
  };

  /* FR-15/FR-16: a block-out ends the game. Any line clear has already
   * resolved — lockPiece() clears rows before spawning (EC-09). The whole
   * board is snapshotted into the explosion particle system and the grid
   * emptied, so the animation consumes every placed block. From here on
   * every gameplay action checks for Phase.FALLING and every key is inert
   * (FR-19/EC-04); there is no path back out of GAME_OVER (FR-19). */
  Game.prototype.enterGameOver = function () {
    this.phase = Phase.GAME_OVER;
    this.currentPiece = null;
    this.paused = false;
    this.gameOverStage = GameOverStage.EXPLODING;
    this.explosion = new global.VT.Explosion.Explosion(
      this.board,
      new global.VT.Explosion.ExplosionOptions()
    );
    this.board.clearAll();
  };

  /* Advance the explosion each frame; when the last shard has vanished
   * the taunt screen remains. Driven from update() under rAF (NFR-03). */
  Game.prototype.updateExplosion = function (elapsedMs) {
    if (
      this.gameOverStage !== GameOverStage.EXPLODING ||
      !this.explosion
    ) {
      return;
    }
    this.explosion.update(elapsedMs);
    if (this.explosion.isComplete()) {
      this.gameOverStage = GameOverStage.TAUNT;
    }
  };

  /* FR-17/FR-18: is the taunt screen up — blast far enough along that
   * "YOU SUCK" and the "New Game" button are readable? Once the TAUNT
   * stage is reached it stays true. The renderer and the button both read
   * this one rule instead of each keeping its own threshold. */
  Game.prototype.showsTaunt = function () {
    if (this.phase !== Phase.GAME_OVER) {
      return false;
    }
    if (this.gameOverStage === GameOverStage.TAUNT || !this.explosion) {
      return true;
    }
    return this.explosion.progress() >= TAUNT_REVEAL_PROGRESS;
  };

  /* Pull the next type off the preview queue and spawn it. A spawn that
   * cannot be placed is a block-out (FR-15). */
  Game.prototype.spawnNext = function () {
    var type = this.nextQueue.shift();
    this.refillQueue();
    var piece = global.VT.Pieces.spawn(type);
    if (!this.board.canPlace(piece)) {
      this.enterGameOver();
      return;
    }
    this.beginPiece(piece);
    this.phase = Phase.FALLING;
    /* FR-14: hold re-enables for each newly spawned piece. */
    this.holdUsed = false;
  };

  /* True while the falling piece can shift one row down. */
  Game.prototype.canMoveDown = function () {
    if (this.phase !== Phase.FALLING || !this.currentPiece) {
      return false;
    }
    var piece = this.currentPiece;
    piece.row += 1;
    var ok = this.board.canPlace(piece);
    piece.row -= 1;
    return ok;
  };

  /* Install a freshly spawned piece and start its fall from a clean
   * slate: no gravity or lock-delay carry-over from the previous piece. */
  Game.prototype.beginPiece = function (piece) {
    this.currentPiece = piece;
    this.gravityElapsedMs = 0;
    /* FR-10: fresh lock-delay budget for the new piece. */
    this.lockElapsedMs = 0;
    this.lockResetsUsed = 0;
  };

  /* FR-10: keep the lock countdown in step with the piece's footing.
   * A piece that can still move down is airborne — the countdown is
   * cancelled outright and costs no reset, so a piece that leaves the
   * ground and lands again gets the full delay. A piece that cannot move
   * down is grounded — a successful move/rotation restarts the
   * countdown, at most MAX_LOCK_RESETS times per piece; past the cap the
   * countdown keeps running. */
  Game.prototype.refreshLockDelay = function () {
    if (this.canMoveDown()) {
      this.lockElapsedMs = 0;
      return;
    }
    if (this.lockResetsUsed < MAX_LOCK_RESETS) {
      this.lockElapsedMs = 0;
      this.lockResetsUsed += 1;
    }
  };

  /* FR-14/EC-06: hold swaps the falling piece with the hold slot.
   * Empty slot: the current piece moves into hold and the next queued
   * piece spawns (the 7-bag continues unchanged). Occupied slot: the
   * types swap; the incoming piece spawns at the standard position and a
   * blocked swap is a block-out (FR-15). A piece may be held only once
   * until it locks. Returns true when the input was accepted. */
  Game.prototype.hold = function () {
    if (
      this.phase !== Phase.FALLING ||
      !this.currentPiece ||
      this.holdUsed ||
      this.paused
    ) {
      return false;
    }
    var outgoing = this.currentPiece.type;
    if (this.heldType === null) {
      this.heldType = outgoing;
      this.spawnNext();
      /* spawnNext() re-enabled hold for the new piece; FR-14 disables it
       * again — the hold action is spent for this lock cycle. */
      this.holdUsed = true;
      return true;
    }
    var incoming = this.heldType;
    this.heldType = outgoing;
    var piece = global.VT.Pieces.spawn(incoming);
    this.holdUsed = true;
    if (!this.board.canPlace(piece)) {
      this.enterGameOver();
      return true;
    }
    this.beginPiece(piece);
    return true;
  };

  /* FR-13: the piece where the current one would land on a hard drop —
   * same type, column and rotation, dropped straight down until it
   * collides. Returns null outside the FALLING phase. The falling piece
   * is not modified. */
  Game.prototype.ghostPiece = function () {
    if (this.phase !== Phase.FALLING || !this.currentPiece) {
      return null;
    }
    var piece = this.currentPiece;
    var ghost = new global.VT.Pieces.Piece(
      piece.type,
      piece.col,
      piece.row,
      piece.rotation
    );
    while (true) {
      ghost.row += 1;
      if (!this.board.canPlace(ghost)) {
        ghost.row -= 1;
        return ghost;
      }
    }
  };

  /* Upcoming piece types (index 0 = next), for the preview panel and the
   * debug queue readout (FR-20 / AC-04). */
  Game.prototype.nextTypes = function (count) {
    return this.nextQueue.slice(0, count || 1);
  };

  /* Try to shift the falling piece by (dCol, dRow); returns true when it
   * moved, leaves it untouched when the target overlaps wall or stack
   * (FR-04). */
  Game.prototype.tryShift = function (dCol, dRow) {
    if (this.phase !== Phase.FALLING || !this.currentPiece || this.paused) {
      return false;
    }
    var piece = this.currentPiece;
    piece.col += dCol;
    piece.row += dRow;
    if (this.board.canPlace(piece)) {
      return true;
    }
    piece.col -= dCol;
    piece.row -= dRow;
    return false;
  };

  Game.prototype.moveLeft = function () {
    var moved = this.tryShift(-1, 0);
    if (moved) {
      this.refreshLockDelay();
    }
    return moved;
  };

  Game.prototype.moveRight = function () {
    var moved = this.tryShift(1, 0);
    if (moved) {
      this.refreshLockDelay();
    }
    return moved;
  };

  /* FR-05/FR-11: Down soft-drops 1 cell per press, worth +1 for that
   * cell. Holding Down is handled by setSoftDropHeld + update(). */
  Game.prototype.softDrop = function () {
    if (!this.tryShift(0, 1)) {
      return false;
    }
    this.refreshLockDelay();
    this.score += SOFT_DROP_SCORE_PER_CELL;
    this.softDropDescentCells += 1;
    return true;
  };

  /* FR-05/FR-11: Down held -> accelerated gravity; each cell gravity
   * descends while held also scores +1. Releasing resets the descent
   * accumulator (the score already awarded stays). */
  Game.prototype.setSoftDropHeld = function (held) {
    this.softDropHeld = held;
    if (!held) {
      this.softDropDescentCells = 0;
    }
  };

  /* FR-10/FR-11: instant drop to the landing row, +2 per cell fallen
   * (not level-multiplied), then lock immediately. */
  Game.prototype.hardDrop = function () {
    if (this.phase !== Phase.FALLING || !this.currentPiece || this.paused) {
      return false;
    }
    var cells = 0;
    while (this.tryShift(0, 1)) {
      cells += 1;
    }
    this.score += cells * HARD_DROP_SCORE_PER_CELL;
    this.lockPiece();
    return true;
  };

  /* EC-03: P/Esc toggles pause; only a falling game can be paused. */
  Game.prototype.togglePause = function () {
    if (this.phase !== Phase.FALLING) {
      return false;
    }
    this.paused = !this.paused;
    return true;
  };

  /* EC-08: focus loss auto-pauses (no toggle — resuming is deliberate). */
  Game.prototype.pause = function () {
    if (this.phase !== Phase.FALLING) {
      return false;
    }
    this.paused = true;
    return true;
  };

  /* FR-05/FR-07: rotate the falling piece with SRS wall kicks. The kick
   * search lives in VT.Rotation; a rotation whose kicks all fail is
   * rejected and the piece is left untouched (EC-01). */
  Game.prototype.rotateCW = function () {
    if (this.phase !== Phase.FALLING || !this.currentPiece || this.paused) {
      return false;
    }
    var rotated = global.VT.Rotation.tryRotate(
      this.currentPiece,
      global.VT.Rotation.Direction.CW,
      this.board
    );
    if (rotated) {
      this.refreshLockDelay();
    }
    return rotated;
  };

  Game.prototype.rotateCCW = function () {
    if (this.phase !== Phase.FALLING || !this.currentPiece || this.paused) {
      return false;
    }
    var rotated = global.VT.Rotation.tryRotate(
      this.currentPiece,
      global.VT.Rotation.Direction.CCW,
      this.board
    );
    if (rotated) {
      this.refreshLockDelay();
    }
    return rotated;
  };

  /* Drive gravity: accumulate elapsed ms and step 1 row per full
   * interval (faster while Down is held). A grounded piece stops falling
   * and runs the FR-10 lock-delay countdown instead. Elapsed time is
   * clamped to MAX_TICK_MS so a backgrounded tab cannot jump-drop the
   * piece on return (EC-08); pause freezes everything (EC-03). */
  Game.prototype.update = function (elapsedMs) {
    if (this.phase === Phase.GAME_OVER) {
      this.updateExplosion(elapsedMs);
      return;
    }
    if (this.phase !== Phase.FALLING || this.paused) {
      return;
    }
    var elapsed = Math.min(elapsedMs, MAX_TICK_MS);
    if (!this.canMoveDown()) {
      this.lockElapsedMs += elapsed;
      if (this.lockElapsedMs >= LOCK_DELAY_MS) {
        this.lockPiece();
      }
      return;
    }
    /* Airborne: a countdown left over from an earlier landing must not
     * survive the fall (FR-10). */
    this.lockElapsedMs = 0;
    var interval = gravityIntervalMs(this.level);
    if (this.softDropHeld) {
      interval = Math.min(interval, SOFT_DROP_GRAVITY_MS);
    }
    this.gravityElapsedMs += elapsed;
    while (this.gravityElapsedMs >= interval) {
      this.gravityElapsedMs -= interval;
      if (!this.tryShift(0, 1)) {
        break;
      }
      if (this.softDropHeld) {
        this.score += SOFT_DROP_SCORE_PER_CELL;
        this.softDropDescentCells += 1;
      }
    }
  };

  /* Lock where it stands, clear full rows, score, update level, spawn the
   * next piece. */
  Game.prototype.lockPiece = function () {
    /* FR-11: the soft-drop descent accumulator resets on lock. */
    this.softDropHeld = false;
    this.softDropDescentCells = 0;
    this.board.lockPiece(this.currentPiece);
    var cleared = this.board.clearFullRows();
    if (cleared > 0) {
      this.lines += cleared;
      this.score += LINE_CLEAR_SCORES[cleared] * this.level;
      /* FR-12: level = 1 + floor(lines / 10); gravity reads it per tick. */
      this.level = 1 + Math.floor(this.lines / LINES_PER_LEVEL);
    }
    this.spawnNext();
  };

  global.VT.Game = Object.freeze({
    Phase: Phase,
    GameOverStage: GameOverStage,
    BASE_GRAVITY_MS: BASE_GRAVITY_MS,
    GRAVITY_LEVEL_DECAY: GRAVITY_LEVEL_DECAY,
    MIN_GRAVITY_MS: MIN_GRAVITY_MS,
    LINES_PER_LEVEL: LINES_PER_LEVEL,
    QUEUE_LOOKAHEAD: QUEUE_LOOKAHEAD,
    LINE_CLEAR_SCORES: LINE_CLEAR_SCORES,
    LOCK_DELAY_MS: LOCK_DELAY_MS,
    MAX_LOCK_RESETS: MAX_LOCK_RESETS,
    SOFT_DROP_SCORE_PER_CELL: SOFT_DROP_SCORE_PER_CELL,
    HARD_DROP_SCORE_PER_CELL: HARD_DROP_SCORE_PER_CELL,
    SOFT_DROP_GRAVITY_MS: SOFT_DROP_GRAVITY_MS,
    MAX_TICK_MS: MAX_TICK_MS,
    TAUNT_REVEAL_PROGRESS: TAUNT_REVEAL_PROGRESS,
    gravityIntervalMs: gravityIntervalMs,
    GameOptions: GameOptions,
    Game: Game,
  });
})(typeof window !== 'undefined' ? window : globalThis);
