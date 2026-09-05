/* pieces.js — tetromino pieces in play: their spawn placement and the
 * absolute board cells of a piece at its position and rotation state.
 *
 * The shape and rotation-state tables live in VT.Rotation (slice 4);
 * colors live in VT.TetrominoColors. Load order: rotation.js before this
 * file.
 */
(function (global) {
  'use strict';

  /* Spawn-state shapes, re-exported from VT.Rotation for consumers that
   * only need the standard spawn orientation (FR-02). */
  var SHAPES = global.VT.Rotation.SPAWN_SHAPES;

  /* FR-01: spawn at the top center. Every origin sits at column 4, row 0;
   * the shape offsets place the cells (T in 3-5, I in 3-6, O in 4-5). */
  var SPAWN_COL = 4;
  var SPAWN_ROW = 0;

  /* Piece: a tetromino in play. `col`/`row` locate the piece's origin cell
   * on the board (row 0 = top visible row); `rotation` is the SRS rotation
   * state 0..3 (0 = spawn orientation). Shape offsets for the current
   * state are added to the origin to get absolute cells. */
  function Piece(type, col, row, rotation) {
    this.type = type;
    this.col = col;
    this.row = row;
    this.rotation = rotation || 0;
  }

  Piece.prototype.absoluteCells = function () {
    var shape = global.VT.Rotation.cellsFor(this.type, this.rotation);
    var originCol = this.col;
    var originRow = this.row;
    return shape.map(function (offset) {
      return { col: originCol + offset.col, row: originRow + offset.row };
    });
  };

  /* Create a fresh piece of `type` at its spawn position. */
  function spawn(type) {
    return new Piece(type, SPAWN_COL, SPAWN_ROW);
  }

  global.VT.Pieces = Object.freeze({
    SHAPES: SHAPES,
    SPAWN_COL: SPAWN_COL,
    SPAWN_ROW: SPAWN_ROW,
    Piece: Piece,
    spawn: spawn,
  });
})(typeof window !== 'undefined' ? window : globalThis);
