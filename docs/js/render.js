/* render.js — canvas drawing: the outlined 10x20 playfield grid, locked
 * board cells, the translucent ghost piece, the Next-piece preview panel,
 * the Hold panel, and the READY / GAME OVER overlays: the explosion's
 * shard debris and the "YOU SUCK" taunt (slice 7).
 *
 * Drawing only: it reads game state passed in by main.js and mutates no
 * module-level variables.
 *
 * All drawing happens in a fixed *logical* pixel space (playfield 300x600,
 * HUD panels 120x80). fitCanvas maps that space onto whatever CSS box the
 * canvas currently has, so a window resize rescales the picture while the
 * game state — which lives in the Game, not here — is untouched (EC-10).
 */
(function (global) {
  'use strict';

  var VT = global.VT;

  /* CanvasSize: the logical drawing space of one canvas. */
  function CanvasSize(width, height) {
    this.width = width;
    this.height = height;
  }

  /* Logical size of the Next/Hold panels, matching their CSS box in
   * style.css. */
  var PANEL_SIZE = new CanvasSize(120, 80);

  /* Logical size of the playfield: the visible 10x20 grid (FR-01). */
  function playfieldSize() {
    return new CanvasSize(
      VT.Board.COLUMNS * VT.Board.CELL_SIZE_PX,
      VT.Board.ROWS * VT.Board.CELL_SIZE_PX
    );
  }

  /* Fit one canvas to its current CSS box and return its 2D context.
   * The backing store is the CSS size x devicePixelRatio (crisp grid lines)
   * and the context transform maps the logical drawing space onto the CSS
   * box. Called at boot and on every window resize (EC-10); getContext
   * returns the same context object, so callers keep drawing with it. */
  function fitCanvas(canvas, logicalSize) {
    var ratio = global.devicePixelRatio || 1;
    var cssWidth = canvas.clientWidth || logicalSize.width;
    var cssHeight = canvas.clientHeight || logicalSize.height;
    canvas.width = Math.round(cssWidth * ratio);
    canvas.height = Math.round(cssHeight * ratio);
    var ctx = canvas.getContext('2d');
    ctx.setTransform(
      (cssWidth / logicalSize.width) * ratio,
      0,
      0,
      (cssHeight / logicalSize.height) * ratio,
      0,
      0
    );
    return ctx;
  }

  /* Paint one grid cell as an inset filled square (locked block or the
   * falling piece). `type` is a tetromino key into VT.TetrominoColors. */
  function drawBlock(ctx, col, row, type) {
    var cell = VT.Board.CELL_SIZE_PX;
    ctx.fillStyle = VT.TetrominoColors[type];
    ctx.fillRect(col * cell + 1, row * cell + 1, cell - 2, cell - 2);
  }

  function drawLockedCells(ctx, board) {
    var row, col;
    for (row = 0; row < board.rows; row += 1) {
      for (col = 0; col < board.columns; col += 1) {
        if (board.cells[row][col] !== null) {
          drawBlock(ctx, col, row, board.cells[row][col]);
        }
      }
    }
  }

  /* FR-13: translucent outline of the hard-drop landing spot. Drawn
   * under the falling piece so both stay legible. */
  function drawGhostPiece(ctx, game) {
    var ghost = game.ghostPiece();
    if (!ghost) {
      return;
    }
    var cell = VT.Board.CELL_SIZE_PX;
    var cells = ghost.absoluteCells();
    var i;
    ctx.globalAlpha = 0.25;
    ctx.fillStyle = VT.TetrominoColors[ghost.type];
    for (i = 0; i < cells.length; i += 1) {
      ctx.fillRect(
        cells[i].col * cell + 1,
        cells[i].row * cell + 1,
        cell - 2,
        cell - 2
      );
    }
    ctx.globalAlpha = 0.8;
    ctx.strokeStyle = VT.TetrominoColors[ghost.type];
    ctx.lineWidth = 1;
    for (i = 0; i < cells.length; i += 1) {
      ctx.strokeRect(
        cells[i].col * cell + 1.5,
        cells[i].row * cell + 1.5,
        cell - 3,
        cell - 3
      );
    }
    ctx.globalAlpha = 1;
  }

  function drawCurrentPiece(ctx, game) {
    if (game.phase !== VT.Game.Phase.FALLING || !game.currentPiece) {
      return;
    }
    var cells = game.currentPiece.absoluteCells();
    var i;
    for (i = 0; i < cells.length; i += 1) {
      drawBlock(ctx, cells[i].col, cells[i].row, game.currentPiece.type);
    }
  }

  /* Paint the playfield: background, 10x20 cell grid, locked cells, the
   * falling piece, border (FR-01, FR-20, FR-22). `game` may be omitted to
   * draw the empty field only. */
  function drawPlayfield(ctx, game) {
    var cols = VT.Board.COLUMNS;
    var rows = VT.Board.ROWS;
    var cell = VT.Board.CELL_SIZE_PX;
    var width = cols * cell;
    var height = rows * cell;
    var i;

    ctx.fillStyle = VT.Theme.PLAYFIELD_BACKGROUND;
    ctx.fillRect(0, 0, width, height);

    ctx.strokeStyle = VT.Theme.GRID_LINE;
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (i = 1; i < cols; i += 1) {
      ctx.moveTo(i * cell + 0.5, 0);
      ctx.lineTo(i * cell + 0.5, height);
    }
    for (i = 1; i < rows; i += 1) {
      ctx.moveTo(0, i * cell + 0.5);
      ctx.lineTo(width, i * cell + 0.5);
    }
    ctx.stroke();

    if (game) {
      drawLockedCells(ctx, game.board);
      drawGhostPiece(ctx, game);
      drawCurrentPiece(ctx, game);
      if (game.phase === VT.Game.Phase.READY) {
        drawOverlayText(ctx, 'PRESS ANY KEY');
      } else if (game.phase === VT.Game.Phase.GAME_OVER) {
        drawGameOverScreen(ctx, game);
      } else if (game.paused) {
        drawOverlayText(ctx, 'PAUSED');
      }
    }

    ctx.strokeStyle = VT.Theme.PLAYFIELD_BORDER;
    ctx.strokeRect(0.5, 0.5, width - 1, height - 1);
  }

  /* Paint an empty Next/Hold panel background (logical panel space). */
  function drawPiecePanel(ctx) {
    var width = PANEL_SIZE.width;
    var height = PANEL_SIZE.height;
    ctx.fillStyle = VT.Theme.PANEL_BACKGROUND;
    ctx.fillRect(0, 0, width, height);
    ctx.strokeStyle = VT.Theme.GRID_LINE;
    ctx.strokeRect(0.5, 0.5, width - 1, height - 1);
    return { width: width, height: height };
  }

  /* Paint a tetromino of `type` centered in the box at a reduced cell
   * size — used by the Next preview (FR-20) and, later, the Hold panel. */
  function drawPiecePreview(ctx, type, boxWidth, boxHeight, cellPx) {
    var shape = VT.Pieces.SHAPES[type];
    var minCol = Infinity;
    var maxCol = -Infinity;
    var minRow = Infinity;
    var maxRow = -Infinity;
    var i;
    var offset;
    for (i = 0; i < shape.length; i += 1) {
      offset = shape[i];
      minCol = Math.min(minCol, offset.col);
      maxCol = Math.max(maxCol, offset.col);
      minRow = Math.min(minRow, offset.row);
      maxRow = Math.max(maxRow, offset.row);
    }
    var originX = (boxWidth - (maxCol - minCol + 1) * cellPx) / 2;
    var originY = (boxHeight - (maxRow - minRow + 1) * cellPx) / 2;
    for (i = 0; i < shape.length; i += 1) {
      offset = shape[i];
      ctx.fillStyle = VT.TetrominoColors[type];
      ctx.fillRect(
        originX + (offset.col - minCol) * cellPx + 1,
        originY + (offset.row - minRow) * cellPx + 1,
        cellPx - 2,
        cellPx - 2
      );
    }
  }

  /* Paint the Next panel: background plus the upcoming piece (FR-20,
   * AC-04 — always game.nextTypes(1), the piece that spawns next). */
  function drawNextPanel(ctx, game) {
    var box = drawPiecePanel(ctx);
    if (!game || game.phase === VT.Game.Phase.READY) {
      return;
    }
    var next = game.nextTypes(1)[0];
    if (next) {
      drawPiecePreview(ctx, next, box.width, box.height, 16);
    }
  }

  /* Paint the Hold panel: background plus the held tetromino, empty while
   * the slot is free (FR-20). */
  function drawHoldPanel(ctx, game) {
    var box = drawPiecePanel(ctx);
    if (!game || game.phase === VT.Game.Phase.READY || !game.heldType) {
      return;
    }
    drawPiecePreview(ctx, game.heldType, box.width, box.height, 16);
  }

  /* FR-16: the shattered blocks — each shard is a small square that
   * flashes white just after the blast, then wears its block's color and
   * fades out while flying (simulation lives in explosion.js). */
  function drawExplosion(ctx, explosion) {
    var particles = explosion.particles;
    var i;
    for (i = 0; i < particles.length; i += 1) {
      var shard = particles[i];
      ctx.globalAlpha = shard.alpha();
      ctx.fillStyle = shard.isFlashing() ? '#ffffff' : shard.color;
      ctx.fillRect(
        shard.x - shard.sizePx / 2,
        shard.y - shard.sizePx / 2,
        shard.sizePx,
        shard.sizePx
      );
    }
    ctx.globalAlpha = 1;
  }

  /* FR-17: "YOU SUCK" — large, centered, red with a white outline on a
   * dark band, legible over the wreckage. */
  function drawTaunt(ctx) {
    var width = VT.Board.COLUMNS * VT.Board.CELL_SIZE_PX;
    var height = VT.Board.ROWS * VT.Board.CELL_SIZE_PX;
    ctx.fillStyle = 'rgba(13, 17, 23, 0.75)';
    ctx.fillRect(0, height / 2 - 55, width, 110);
    ctx.font = 'bold 58px monospace';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.lineWidth = 6;
    ctx.strokeStyle = VT.Theme.TEXT_PRIMARY;
    ctx.strokeText(TAUNT_TEXT, width / 2, height / 2);
    ctx.fillStyle = VT.Theme.TAUNT;
    ctx.fillText(TAUNT_TEXT, width / 2, height / 2);
  }

  /* Game-over screen: the debris field, with the taunt revealed once the
   * blast is far enough along to stay readable (FR-17 allows during or
   * after the explosion) and kept for good in the TAUNT stage (FR-19). */
  function drawGameOverScreen(ctx, game) {
    if (game.explosion) {
      drawExplosion(ctx, game.explosion);
    }
    if (game.showsTaunt()) {
      drawTaunt(ctx);
    }
  }

  /* Centered full-playfield message (start prompt, PAUSED). */
  function drawOverlayText(ctx, text) {
    var width = VT.Board.COLUMNS * VT.Board.CELL_SIZE_PX;
    var height = VT.Board.ROWS * VT.Board.CELL_SIZE_PX;
    ctx.fillStyle = 'rgba(13, 17, 23, 0.8)';
    ctx.fillRect(0, 0, width, height);
    ctx.fillStyle = VT.Theme.TEXT_PRIMARY;
    ctx.font = 'bold 30px monospace';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(text, width / 2, height / 2);
  }

  /* FR-17 taunt text. */
  var TAUNT_TEXT = 'YOU SUCK';

  VT.Render = Object.freeze({
    CanvasSize: CanvasSize,
    PANEL_SIZE: PANEL_SIZE,
    playfieldSize: playfieldSize,
    fitCanvas: fitCanvas,
    drawPlayfield: drawPlayfield,
    drawPiecePanel: drawPiecePanel,
    drawPiecePreview: drawPiecePreview,
    drawNextPanel: drawNextPanel,
    drawHoldPanel: drawHoldPanel,
    drawOverlayText: drawOverlayText,
    drawExplosion: drawExplosion,
    drawTaunt: drawTaunt,
    TAUNT_TEXT: TAUNT_TEXT,
  });
})(typeof window !== 'undefined' ? window : globalThis);
