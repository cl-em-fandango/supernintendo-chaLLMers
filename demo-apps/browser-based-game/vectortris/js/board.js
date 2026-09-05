/* board.js — playfield grid state: dimensions, occupancy, collision and
 * line clearing. Pure state + predicates over it; scoring, gravity and the
 * phase machine live in game.js, drawing in render.js.
 */
(function (global) {
  'use strict';

  /* Shape of the visible playfield (FR-01: exactly 10 columns x 20 rows). */
  var COLUMNS = 10;
  var ROWS = 20;
  var CELL_SIZE_PX = 30;

  /* GameBoard: rows x cols grid. Each cell is null (empty) or a tetromino
   * type string (a key into VT.TetrominoColors). Row 0 is the top visible
   * row; row ROWS-1 is the floor. */
  function GameBoard() {
    this.columns = COLUMNS;
    this.rows = ROWS;
    this.cells = [];
    var row;
    for (row = 0; row < this.rows; row += 1) {
      this.cells.push(new Array(this.columns).fill(null));
    }
  }

  GameBoard.prototype.isInside = function (col, row) {
    return col >= 0 && col < this.columns && row >= 0 && row < this.rows;
  };

  GameBoard.prototype.isOccupied = function (col, row) {
    return this.cells[row][col] !== null;
  };

  /* FR-04: a piece may occupy only inside, empty cells. */
  GameBoard.prototype.canPlace = function (piece) {
    var cells = piece.absoluteCells();
    var i;
    for (i = 0; i < cells.length; i += 1) {
      if (!this.isInside(cells[i].col, cells[i].row)) {
        return false;
      }
      if (this.isOccupied(cells[i].col, cells[i].row)) {
        return false;
      }
    }
    return true;
  };

  /* Write the piece into the grid. Caller guarantees canPlace(piece). */
  GameBoard.prototype.lockPiece = function (piece) {
    var cells = piece.absoluteCells();
    var i;
    for (i = 0; i < cells.length; i += 1) {
      this.cells[cells[i].row][cells[i].col] = piece.type;
    }
  };

  GameBoard.prototype.isRowFull = function (row) {
    var col;
    for (col = 0; col < this.columns; col += 1) {
      if (this.cells[row][col] === null) {
        return false;
      }
    }
    return true;
  };

  /* FR-11: every fully filled row disappears simultaneously; rows above
   * shift down. Returns the number of rows cleared (0-4). */
  GameBoard.prototype.clearFullRows = function () {
    var cleared = 0;
    var row;
    for (row = this.rows - 1; row >= 0; row -= 1) {
      if (this.isRowFull(row)) {
        this.cells.splice(row, 1);
        this.cells.unshift(new Array(this.columns).fill(null));
        cleared += 1;
        row += 1; /* rows slid down into this index — re-check it */
      }
    }
    return cleared;
  };

  /* Empty the whole grid — the game-over explosion has snapshotted the
   * blocks and now renders their debris instead (game.js). */
  GameBoard.prototype.clearAll = function () {
    var row;
    for (row = 0; row < this.rows; row += 1) {
      this.cells[row] = new Array(this.columns).fill(null);
    }
  };

  global.VT.Board = Object.freeze({
    COLUMNS: COLUMNS,
    ROWS: ROWS,
    CELL_SIZE_PX: CELL_SIZE_PX,
    GameBoard: GameBoard,
  });
})(typeof window !== 'undefined' ? window : globalThis);
