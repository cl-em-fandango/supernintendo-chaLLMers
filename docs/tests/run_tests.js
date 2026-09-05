/* run_tests.js — Node test runner for the pure-logic Vectortris modules
 * (board, rotation, pieces, rng, game, input). The logic files are plain <script>
 * IIFEs that attach to `window` in the browser and to `globalThis` here, so
 * we evaluate them in one shared VM context and assert against the VT
 * namespace. Run with: node tests/run_tests.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const APP_DIR = path.join(__dirname, '..');
const JS_DIR = path.join(APP_DIR, 'js');
const LOGIC_FILES = [
  'constants.js',
  'rotation.js',
  'board.js',
  'pieces.js',
  'rng.js',
  'explosion.js',
  'game.js',
  'new_game_button.js',
  'render.js',
  'input.js',
];

/* The shared VM context, kept so tests can set browser globals such as
 * devicePixelRatio that render.js reads at call time. */
let MODULE_CONTEXT = null;

function loadModules() {
  const context = vm.createContext({});
  vm.runInContext('var window = undefined;', context);
  for (const file of LOGIC_FILES) {
    const source = fs.readFileSync(path.join(JS_DIR, file), 'utf8');
    vm.runInContext(source, context, { filename: file });
  }
  MODULE_CONTEXT = context;
  return context.VT;
}

let failures = 0;

function check(name, condition) {
  if (condition) {
    console.log('ok   - ' + name);
  } else {
    failures += 1;
    console.log('FAIL - ' + name);
  }
}

function cellSet(cells) {
  return cells.map((c) => c.col + ',' + c.row).sort().join(' ');
}

function sortedJoin(arr) {
  return arr.slice().sort().join(',');
}

function fillRow(board, row, exceptCols) {
  for (let col = 0; col < board.columns; col += 1) {
    if (!exceptCols || !exceptCols.includes(col)) {
      board.cells[row][col] = 'Z';
    }
  }
}

/* Always returns 0 -> Fisher-Yates degenerates to a fixed permutation:
 * the bag order is deterministically O, T, S, Z, J, L, I. */
function zeroRandom() {
  return () => 0;
}

function makeGame() {
  return new VT.Game.Game(new VT.Game.GameOptions(zeroRandom()));
}

const VT = loadModules();
const BAG_ORDER = ['O', 'T', 'S', 'Z', 'J', 'L', 'I'];

/* update() clamps each call to MAX_TICK_MS (EC-08), so tests feed time
 * in clamped chunks. */
function runMs(game, totalMs) {
  let remaining = totalMs;
  while (remaining > 0) {
    game.update(Math.min(remaining, VT.Game.MAX_TICK_MS));
    remaining -= VT.Game.MAX_TICK_MS;
  }
}

/* --- board.js ------------------------------------------------------- */

const board = new VT.Board.GameBoard();
check('board is 10x20', board.columns === 10 && board.rows === 20);
check(
  'board starts empty',
  board.cells.every((row) => row.every((c) => c === null))
);

/* --- pieces.js: all 7 types, standard spawn placement --------------- */

check(
  'all 7 tetrominoes defined',
  sortedJoin(Object.keys(VT.Pieces.SHAPES)) ===
    sortedJoin(VT.Rng.PIECE_TYPES)
);
check(
  'all 7 tetrominoes have colors (FR-02)',
  VT.Rng.PIECE_TYPES.every((t) => typeof VT.TetrominoColors[t] === 'string')
);
for (const type of VT.Rng.PIECE_TYPES) {
  const piece = VT.Pieces.spawn(type);
  const cells = piece.absoluteCells();
  check(
    type + ': spawn has 4 cells inside the field at the top',
    cells.length === 4 &&
      cells.every((c) => c.col >= 0 && c.col < 10 && c.row >= 0 && c.row < 20) &&
      cells.some((c) => c.row === 0)
  );
}
check(
  'I spawns in columns 3-6',
  cellSet(VT.Pieces.spawn('I').absoluteCells()) === '3,0 4,0 5,0 6,0'
);
check(
  'O spawns in columns 4-5',
  cellSet(VT.Pieces.spawn('O').absoluteCells()) === '4,0 4,1 5,0 5,1'
);
check(
  'T spawns top-center',
  cellSet(VT.Pieces.spawn('T').absoluteCells()) ===
    '4,0 3,1 4,1 5,1'.split(' ').sort().join(' ')
);

/* --- board.canPlace -------------------------------------------------- */

const tPiece = VT.Pieces.spawn('T');
check('canPlace: fresh spawn is valid', board.canPlace(tPiece));
tPiece.col = 0;
check('canPlace: rejected at left wall', !board.canPlace(tPiece));
tPiece.col = 4;
board.cells[1][4] = 'Z';
check('canPlace: rejected on occupied cell', !board.canPlace(tPiece));
board.cells[1][4] = null;

/* --- rng.js: 7-bag (FR-03 / EC-07) ----------------------------------- */

const bag = new VT.Rng.SevenBag(zeroRandom());
check(
  'deterministic bag order',
  BAG_ORDER.join(',') ===
    Array.from({ length: 7 }, () => bag.next()).join(',')
);
const liveBag = new VT.Rng.SevenBag(Math.random);
const drawn = Array.from({ length: 21 }, () => liveBag.next());
check(
  'every consecutive group of 7 draws is a full permutation',
  [0, 7, 14].every((base) =>
    sortedJoin(drawn.slice(base, base + 7)) === sortedJoin(VT.Rng.PIECE_TYPES)
  )
);
const peekBag = new VT.Rng.SevenBag(zeroRandom());
const peeked = peekBag.upcoming(3);
check(
  'upcoming() peeks without consuming',
  peeked.join(',') === 'O,T,S' && peekBag.next() === 'O'
);

/* --- game.js: READY start screen (FR-05 / AC-11) ---------------------- */

const readyGame = makeGame();
check('game starts on the READY screen', readyGame.phase === VT.Game.Phase.READY);
check('no falling piece before start', readyGame.currentPiece === null);
check(
  'preview queue is pre-filled from the bag',
  readyGame.nextTypes(5).join(',') === 'O,T,S,Z,J'
);
check('gravity does nothing before start', (readyGame.update(5000), readyGame.currentPiece === null));
check('start() begins the game', readyGame.start() && readyGame.phase === VT.Game.Phase.FALLING);
check('first piece is the first previewed type', readyGame.currentPiece.type === 'O');
check('start() is refused mid-game', readyGame.start() === false);

/* --- game.js: movement + collision ------------------------------------ */

const wallGame = makeGame();
wallGame.start();
let leftMoves = 0;
while (wallGame.moveLeft() && leftMoves < 20) {
  leftMoves += 1;
}
const restingCells = wallGame.currentPiece.absoluteCells();
check(
  'piece rests flush against the left wall (FR-04)',
  Math.min(...restingCells.map((c) => c.col)) === 0
);
check('moveRight works', wallGame.moveRight());

/* --- game.js: soft drop ------------------------------------------------ */

const dropGame = makeGame();
dropGame.start();
const rowBefore = dropGame.currentPiece.row;
check('softDrop moves 1 cell', dropGame.softDrop() &&
  dropGame.currentPiece.row === rowBefore + 1);

/* --- game.js: gravity + level curve (FR-09 / FR-12) ------------------- */

check('level 1 gravity is 1000 ms', VT.Game.gravityIntervalMs(1) === 1000);
check('level 2 gravity is faster', VT.Game.gravityIntervalMs(2) === 850);
check(
  'gravity keeps decreasing and clamps at the floor',
  VT.Game.gravityIntervalMs(3) < VT.Game.gravityIntervalMs(2) &&
    VT.Game.gravityIntervalMs(50) === VT.Game.MIN_GRAVITY_MS
);

const gravityGame = makeGame();
gravityGame.start(); /* seeded O piece: cells rows 0-1, cols 4-5 */
runMs(gravityGame, VT.Game.gravityIntervalMs(1) - 1);
check('no fall before interval elapses', gravityGame.currentPiece.row === 0);
runMs(gravityGame, 1);
check('falls 1 row per interval', gravityGame.currentPiece.row === 1);

/* O spans 2 rows, so its origin rests at row 18 (cells rows 18-19). */
for (let i = 0; i < 30 && gravityGame.currentPiece.row < 18; i += 1) {
  runMs(gravityGame, VT.Game.gravityIntervalMs(1));
}
check('piece rests on the floor', gravityGame.currentPiece.row === 18);
/* FR-10: a grounded piece locks after the lock delay, not on landing. */
runMs(gravityGame, VT.Game.LOCK_DELAY_MS);
check(
  'locked blocks stay on the board',
  gravityGame.board.cells[19][4] === 'O' &&
    gravityGame.board.cells[19][5] === 'O' &&
    gravityGame.board.cells[18][4] === 'O' &&
    gravityGame.board.cells[18][5] === 'O'
);
check(
  'next bag type spawns after lock',
  gravityGame.phase === VT.Game.Phase.FALLING &&
    gravityGame.currentPiece.type === 'T' &&
    gravityGame.currentPiece.row === 0 &&
    gravityGame.currentPiece.col === 4
);

/* --- board.clearFullRows ----------------------------------------------- */

const clearBoard = new VT.Board.GameBoard();
fillRow(clearBoard, 19);
clearBoard.cells[18][2] = 'J';
check('clearFullRows clears the full row', clearBoard.clearFullRows() === 1);
check('cleared blocks are gone',
  clearBoard.cells[19].every((c, col) => col === 2 || c === null));
check(
  'rows above shift down',
  clearBoard.cells[19][2] === 'J' && clearBoard.cells[18][2] === null
);

const multiBoard = new VT.Board.GameBoard();
fillRow(multiBoard, 19);
fillRow(multiBoard, 18);
check('two full rows clear at once', multiBoard.clearFullRows() === 2);
check('multi-clear leaves empty rows', multiBoard.cells[19].every((c) => c === null) &&
  multiBoard.cells[18].every((c) => c === null));

/* --- game.js: scoring + level (FR-11 / FR-12) -------------------------- */

const scoreGame = makeGame();
scoreGame.start(); /* O piece, cols 4-5 */
fillRow(scoreGame.board, 19, [4, 5]); /* gap where the O will land */
scoreGame.currentPiece.row = 18;
scoreGame.lockPiece();
check('single clear scores 100 x level', scoreGame.score === 100);
check('lines counter increments', scoreGame.lines === 1);
check('level stays 1 below 10 lines', scoreGame.level === 1);
check(
  'cleared row has no Z blocks left after lock',
  scoreGame.board.cells[19].every((c) => c !== 'Z')
);

/* Second piece is T (cols 3-5 on its bottom row); pretend 9 lines so this
 * clear crosses the 10-line level boundary. */
scoreGame.lines = 9;
fillRow(scoreGame.board, 19, [3, 4, 5]);
scoreGame.currentPiece.row = 18;
scoreGame.lockPiece();
check('10th cleared line raises level to 2', scoreGame.level === 2 && scoreGame.lines === 10);

/* --- game.js: preview always matches the spawn (AC-04) ----------------- */

const previewGame = makeGame();
previewGame.start();
const spawnedTypes = [previewGame.currentPiece.type];
let previewOk = true;
for (let i = 0; i < 6; i += 1) {
  const preview = previewGame.nextTypes(1)[0];
  while (previewGame.moveLeft()) { /* park against the left wall */ }
  const falling = previewGame.currentPiece;
  while (
    previewGame.currentPiece === falling &&
    previewGame.phase === VT.Game.Phase.FALLING
  ) {
    runMs(previewGame, VT.Game.gravityIntervalMs(previewGame.level) + VT.Game.LOCK_DELAY_MS);
  }
  previewOk = previewOk && previewGame.currentPiece.type === preview;
  spawnedTypes.push(previewGame.currentPiece.type);
}
check('next preview always matches the piece that spawns', previewOk);
check(
  'one full bag spawns every type exactly once (FR-03/EC-07)',
  sortedJoin(spawnedTypes) === sortedJoin(VT.Rng.PIECE_TYPES)
);

/* --- game.js: block-out game over (FR-15) ------------------------------ */

const overGame = makeGame();
overGame.start();
fillRow(overGame.board, 0);
fillRow(overGame.board, 1);
overGame.spawnNext();
check('blocked spawn enters GAME_OVER', overGame.phase === VT.Game.Phase.GAME_OVER);
check('no actions after game over', overGame.moveLeft() === false);
check('gravity frozen after game over', (overGame.update(60000), overGame.phase === VT.Game.Phase.GAME_OVER));
check('no restart after game over (FR-19)', overGame.start() === false);

/* --- explosion.js: particle system (FR-16) ---------------------------- */

check(
  'explosion duration sits in the 2-5 s band (FR-16)',
  VT.Explosion.DURATION_MS >= 2000 && VT.Explosion.DURATION_MS <= 5000
);

const blastBoard = new VT.Board.GameBoard();
blastBoard.cells[19][0] = 'Z';
blastBoard.cells[10][5] = 'I';
const blast = new VT.Explosion.Explosion(
  blastBoard,
  new VT.Explosion.ExplosionOptions(
    VT.Explosion.DURATION_MS,
    4,
    zeroRandom()
  )
);
check(
  'every placed block shatters into shards',
  blast.blockCount === 2 &&
    blast.particles.length === 2 * 4
);
check(
  'shards wear the color of the block they came from (FR-02 colors)',
  blast.particles.every(
    (p) =>
      p.color === VT.TetrominoColors.Z || p.color === VT.TetrominoColors.I
  )
);
check(
  'the snapshot does not modify the board',
  blastBoard.cells[19][0] === 'Z' && blastBoard.cells[10][5] === 'I'
);
check(
  'fresh explosion: full progress 0, nothing complete',
  blast.progress() === 0 && !blast.isComplete()
);
const shardBefore = { x: blast.particles[0].x, y: blast.particles[0].y };
blast.update(100);
check(
  'shards fly outward as the simulation runs',
  blast.particles[0].x !== shardBefore.x || blast.particles[0].y !== shardBefore.y
);
check(
  'shards flash white at the blast, then wear the block color (FR-16)',
  blast.particles[0].isFlashing() === true
);
blast.update(VT.Explosion.FLASH_MS);
check(
  'the flash ends after FLASH_MS',
  blast.particles[0].isFlashing() === false
);
runMs({
  update: (ms) => blast.update(ms),
}, VT.Explosion.DURATION_MS);
check(
  'the whole board is consumed by the end of the explosion (AC-05)',
  blast.isComplete() && blast.progress() === 1 && blast.particles.length === 0
);

/* --- game.js: game-over explosion + taunt (FR-16/FR-17/FR-19) --------- */

const boomGame = makeGame();
boomGame.start();
fillRow(boomGame.board, 19);
fillRow(boomGame.board, 0);
fillRow(boomGame.board, 1);
boomGame.spawnNext(); /* blocked spawn -> block-out */
check(
  'block-out enters GAME_OVER in the EXPLODING stage',
  boomGame.phase === VT.Game.Phase.GAME_OVER &&
    boomGame.gameOverStage === VT.Game.GameOverStage.EXPLODING
);
check(
  'the explosion snapshots every placed block',
  boomGame.explosion !== null && boomGame.explosion.blockCount === 30
);
check(
  'the grid is emptied — the debris is all that is left to draw',
  boomGame.board.cells.every((row) => row.every((c) => c === null))
);
runMs(boomGame, VT.Explosion.DURATION_MS - 1);
check(
  'the explosion is still running just under its duration',
  boomGame.gameOverStage === VT.Game.GameOverStage.EXPLODING &&
    boomGame.explosion.progress() < 1 &&
    !boomGame.explosion.isComplete()
);
runMs(boomGame, 1);
check(
  'after ~2-5 s the explosion is over and the taunt remains',
  boomGame.gameOverStage === VT.Game.GameOverStage.TAUNT &&
    boomGame.explosion.particles.length === 0
);
runMs(boomGame, 60000);
check(
  'the taunt stage is terminal — update changes nothing (FR-19)',
  boomGame.phase === VT.Game.Phase.GAME_OVER &&
    boomGame.gameOverStage === VT.Game.GameOverStage.TAUNT
);
check(
  'no gameplay action works during the explosion',
  boomGame.moveLeft() === false &&
    boomGame.rotateCW() === false &&
    boomGame.hardDrop() === false &&
    boomGame.hold() === false &&
    boomGame.togglePause() === false &&
    boomGame.pause() === false &&
    boomGame.ghostPiece() === null
);

/* EC-09: a lock that clears lines AND blocks the next spawn resolves the
 * line clear (score, lines, level) before the explosion starts. */
const ec9Game = makeGame();
ec9Game.start(); /* seeded O piece, cols 4-5 */
fillRow(ec9Game.board, 19, [4, 5]); /* the O completes this row */
fillRow(ec9Game.board, 0, [0]); /* not full: they survive the clear and */
fillRow(ec9Game.board, 1, [0]); /* slide into rows 1-2 to block the spawn */
ec9Game.currentPiece.row = 18;
ec9Game.lockPiece();
check(
  'EC-09: the line clear resolves first, then game over',
  ec9Game.lines === 1 &&
    ec9Game.score === VT.Game.LINE_CLEAR_SCORES[1] &&
    ec9Game.phase === VT.Game.Phase.GAME_OVER &&
    ec9Game.gameOverStage === VT.Game.GameOverStage.EXPLODING
);
check(
  'EC-09: the explosion consumed the shifted stack, not the cleared row',
  ec9Game.explosion.blockCount === 20 /* 2x9 shifted + the O's survivors */
);

/* A blocked hold swap ends the game the same way. */
const holdBoomGame = makeGame();
holdBoomGame.start();
holdBoomGame.hold(); /* O -> hold, T spawns */
lockCurrent(holdBoomGame); /* lock T -> S spawns, hold re-enabled */
fillRow(holdBoomGame.board, 0);
fillRow(holdBoomGame.board, 1);
check(
  'blocked hold swap explodes too',
  holdBoomGame.hold() === true &&
    holdBoomGame.gameOverStage === VT.Game.GameOverStage.EXPLODING &&
    holdBoomGame.explosion !== null
);

/* --- rotation.js: SRS rotation states + kick tables ------------------ */

const Rotation = VT.Rotation;

check(
  'every type has 4 rotation states of 4 cells',
  VT.Rng.PIECE_TYPES.every(
    (t) =>
      Rotation.STATES[t].length === 4 &&
      Rotation.STATES[t].every((state) => state.length === 4)
  )
);
check(
  'state 0 is the spawn shape (FR-02)',
  VT.Rng.PIECE_TYPES.every(
    (t) => cellSet(Rotation.STATES[t][0]) === cellSet(Rotation.SPAWN_SHAPES[t])
  )
);
check(
  'O rotation states are all identical — O never shifts (FR-07)',
  Rotation.STATES.O.every((s) => cellSet(s) === cellSet(Rotation.STATES.O[0]))
);
check(
  'T state R is the vertical SRS shape',
  cellSet(Rotation.STATES.T[1]) === '0,0 0,1 0,2 1,1'
);
check(
  'I state R is a vertical column',
  cellSet(Rotation.STATES.I[1]) === '1,-1 1,0 1,1 1,2'
);
check(
  'I state 2 is a horizontal row',
  cellSet(Rotation.STATES.I[2]) === '-1,1 0,1 1,1 2,1'
);
check(
  'I uses its own kick table, distinct from JLSTZ (EC-02)',
  JSON.stringify(Rotation.kicksFor('I', 0, 1)) !==
    JSON.stringify(Rotation.kicksFor('T', 0, 1)) &&
    JSON.stringify(Rotation.kicksFor('S', 2, 3)) ===
      JSON.stringify(Rotation.kicksFor('L', 2, 3))
);
check(
  'O kicks are the identity offset only',
  Rotation.kicksFor('O', 0, 1).length === 1 &&
    Rotation.kicksFor('O', 0, 1)[0].dCol === 0 &&
    Rotation.kicksFor('O', 0, 1)[0].dRow === 0
);
check(
  'every transition has 5 kick offsets',
  ['0>1', '1>0', '1>2', '2>1', '2>3', '3>2', '3>0', '0>3'].every(
    (key) =>
      Rotation.KICKS_JLSTZ[key].length === 5 &&
      Rotation.KICKS_I[key].length === 5
  )
);

/* Open-space rotation: every piece turns CW and CCW. */
const openBoard = new VT.Board.GameBoard();
check(
  'all pieces rotate CW and CCW in open space',
  VT.Rng.PIECE_TYPES.every((t) => {
    const piece = new VT.Pieces.Piece(t, 4, 10, 0);
    const cw = Rotation.tryRotate(piece, Rotation.Direction.CW, openBoard);
    const ccw = Rotation.tryRotate(piece, Rotation.Direction.CCW, openBoard);
    return cw && ccw && piece.rotation === 0;
  })
);
const cyclePiece = new VT.Pieces.Piece('T', 4, 10, 0);
const cycleCells = cellSet(cyclePiece.absoluteCells());
for (let i = 0; i < 4; i += 1) {
  Rotation.tryRotate(cyclePiece, Rotation.Direction.CW, openBoard);
}
check(
  'four CW turns return to the spawn state and cells',
  cyclePiece.rotation === 0 &&
    cellSet(cyclePiece.absoluteCells()) === cycleCells
);

/* Wall kick: vertical I flush to the left wall rotates to horizontal via
 * the I table's (+2, 0) offset (EC-02). */
const wallKickBoard = new VT.Board.GameBoard();
const wallI = new VT.Pieces.Piece('I', -1, 1, 1); /* column 0, rows 0-3 */
check('vertical I sits at the left wall', wallKickBoard.canPlace(wallI));
check('I rotates CW at the left wall', Rotation.tryRotate(wallI, Rotation.Direction.CW, wallKickBoard));
check(
  'I wall kick lands horizontal and fully inside',
  wallI.rotation === 2 &&
    wallI.col === 1 &&
    wallI.absoluteCells().every((c) => c.col >= 0 && c.col < 10 && c.row >= 0 && c.row < 20)
);

/* Floor kick: horizontal I on the floor rotates vertical via the I
 * table's (+1, -2) offset, lifting the column fully into the field. */
const floorKickBoard = new VT.Board.GameBoard();
const floorI = new VT.Pieces.Piece('I', 4, 19, 0); /* row 19, cols 3-6 */
check('horizontal I rests on the floor', floorKickBoard.canPlace(floorI));
check('I rotates CW on the floor', Rotation.tryRotate(floorI, Rotation.Direction.CW, floorKickBoard));
check(
  'I floor kick lands vertical at rows 16-19',
  floorI.rotation === 1 &&
    floorI.col === 5 &&
    floorI.row === 17 &&
    cellSet(floorI.absoluteCells()) === '6,16 6,17 6,18 6,19'
);

/* Rejection: a T boxed in on every side is cleanly rejected and left
 * untouched (EC-01). */
const boxedBoard = new VT.Board.GameBoard();
const boxedT = new VT.Pieces.Piece('T', 4, 10, 0);
for (let row = 0; row < 20; row += 1) {
  for (let col = 0; col < 10; col += 1) {
    boxedBoard.cells[row][col] = 'Z';
  }
}
boxedT.absoluteCells().forEach((c) => {
  boxedBoard.cells[c.row][c.col] = null;
});
check(
  'boxed-in rotation is rejected and the piece is untouched',
  Rotation.tryRotate(boxedT, Rotation.Direction.CW, boxedBoard) === false &&
    Rotation.tryRotate(boxedT, Rotation.Direction.CCW, boxedBoard) === false &&
    boxedT.rotation === 0 &&
    boxedT.col === 4 &&
    boxedT.row === 10 &&
    boxedBoard.canPlace(boxedT)
);

/* EC-01 sweep: across every type, state, position and CW rotation on both
 * an empty and a partially filled board, an accepted rotation never
 * overlaps a filled cell or leaves the field; a rejected one never moves
 * the piece. */
function sweepBoard(filled) {
  const board = new VT.Board.GameBoard();
  if (filled) {
    for (let row = 0; row < 20; row += 1) {
      for (let col = 0; col < 10; col += 1) {
        if ((col + row) % 4 === 0) {
          board.cells[row][col] = 'Z';
        }
      }
    }
  }
  return board;
}
let sweepViolations = 0;
let sweepAccepted = 0;
for (const board of [sweepBoard(false), sweepBoard(true)]) {
  for (const t of VT.Rng.PIECE_TYPES) {
    for (let rot = 0; rot < 4; rot += 1) {
      for (let col = -2; col < 12; col += 1) {
        for (let row = -2; row < 22; row += 1) {
          const piece = new VT.Pieces.Piece(t, col, row, rot);
          if (!board.canPlace(piece)) {
            continue;
          }
          const accepted = Rotation.tryRotate(piece, Rotation.Direction.CW, board);
          if (accepted) {
            sweepAccepted += 1;
            const ok = piece.absoluteCells().every(
              (c) =>
                c.col >= 0 && c.col < 10 && c.row >= 0 && c.row < 20 &&
                !board.isOccupied(c.col, c.row)
            );
            if (!ok) {
              sweepViolations += 1;
            }
          } else if (
            piece.rotation !== rot || piece.col !== col || piece.row !== row
          ) {
            sweepViolations += 1;
          }
        }
      }
    }
  }
}
check(
  'EC-01 sweep: no rotation ever overlaps or leaves the field',
  sweepViolations === 0 && sweepAccepted > 100
);

/* --- game.js: rotate actions ------------------------------------------ */

const rotPhaseGame = makeGame();
check('rotate refused before start',
  rotPhaseGame.rotateCW() === false && rotPhaseGame.rotateCCW() === false);
rotPhaseGame.start();
const oPieceCells = cellSet(rotPhaseGame.currentPiece.absoluteCells());
check(
  'rotateCW on the falling O piece keeps its cells (FR-07)',
  rotPhaseGame.rotateCW() &&
    rotPhaseGame.currentPiece.rotation === 1 &&
    cellSet(rotPhaseGame.currentPiece.absoluteCells()) === oPieceCells
);
const rotOverGame = makeGame();
rotOverGame.start();
fillRow(rotOverGame.board, 0);
fillRow(rotOverGame.board, 1);
rotOverGame.spawnNext();
check('rotate refused after game over',
  rotOverGame.rotateCW() === false && rotOverGame.rotateCCW() === false);

/* --- game.js: ghost piece (FR-13 / AC-04) ------------------------------ */

const ghostGame = makeGame();
check('no ghost before start', ghostGame.ghostPiece() === null);
ghostGame.start(); /* seeded O piece: cols 4-5, rows 0-1 */
const floorGhost = ghostGame.ghostPiece();
check(
  'ghost lands on the floor of an empty field',
  floorGhost.type === 'O' &&
    floorGhost.col === 4 &&
    floorGhost.rotation === 0 &&
    cellSet(floorGhost.absoluteCells()) === '4,18 4,19 5,18 5,19'
);
check('ghosting does not move the falling piece', ghostGame.currentPiece.row === 0);
ghostGame.moveLeft();
ghostGame.moveLeft();
check(
  'ghost follows the piece column',
  ghostGame.ghostPiece().absoluteCells().every((c) => c.col === 2 || c.col === 3)
);
fillRow(ghostGame.board, 19);
fillRow(ghostGame.board, 18, [2, 3]); /* ledge under the piece */
check(
  'ghost rests on the stack, not through it',
  ghostGame.ghostPiece().row === 17 &&
    ghostGame.ghostPiece()
      .absoluteCells()
      .every((c) => !ghostGame.board.isOccupied(c.col, c.row))
);
const landGame = makeGame();
landGame.start();
landGame.currentPiece.col = 1;
fillRow(landGame.board, 19);
fillRow(landGame.board, 18, [1, 2]);
const predictedRow = landGame.ghostPiece().row;
while (landGame.softDrop()) { /* drop it for real */ }
check(
  'ghost row equals the real landing row',
  predictedRow === landGame.currentPiece.row
);
check('no ghost after game over', overGame.ghostPiece() === null);

/* --- game.js: hold (FR-14 / EC-06) ------------------------------------- */

/* Park the falling piece on the right wall, drop it to rest and lock it. */
function lockCurrent(game) {
  const type = game.currentPiece.type;
  while (game.moveRight()) { /* wall */ }
  while (game.softDrop()) { /* floor */ }
  game.lockPiece();
  return type;
}

const holdGame = makeGame();
check('hold refused before start', holdGame.hold() === false);
holdGame.start(); /* O spawns; queue T,S,Z,J,L */
check('hold slot starts empty', holdGame.heldType === null);
check('hold with an empty slot is accepted (EC-06)', holdGame.hold());
check('current piece moved into the hold slot', holdGame.heldType === 'O');
check('next queued piece spawns instead', holdGame.currentPiece.type === 'T');
check(
  'the 7-bag queue continues unchanged',
  holdGame.nextTypes(4).join(',') === 'S,Z,J,L'
);
check(
  'second hold before lock does nothing (FR-14)',
  holdGame.hold() === false &&
    holdGame.currentPiece.type === 'T' &&
    holdGame.heldType === 'O'
);
lockCurrent(holdGame); /* lock T -> S spawns and hold re-enables */
check('hold re-enables for the next piece', holdGame.currentPiece.type === 'S');
check(
  'occupied hold swaps current and held',
  holdGame.hold() &&
    holdGame.currentPiece.type === 'O' &&
    holdGame.currentPiece.rotation === 0 &&
    holdGame.currentPiece.col === VT.Pieces.SPAWN_COL &&
    holdGame.heldType === 'S'
);
check(
  'a swap does not touch the spawn queue',
  holdGame.nextTypes(4).join(',') === 'Z,J,L,I'
);
check('swap consumes the hold for this piece', holdGame.hold() === false);

/* Bag invariant across holds: pieces entering play from the bag are
 * O, T, S, Z, J, L, I — the swapped-back O is not a new bag entry
 * (FR-03 / EC-07). */
lockCurrent(holdGame); /* lock the swapped-back O — not a new bag entry */
const seenTypes = ['O', 'T', 'S']; /* O and S entered play via hold */
while (seenTypes.length < 7 && holdGame.phase === VT.Game.Phase.FALLING) {
  seenTypes.push(lockCurrent(holdGame)); /* Z, J, L, I from the bag */
}
check(
  'holds never skip or duplicate a bag entry',
  sortedJoin(seenTypes) === sortedJoin(VT.Rng.PIECE_TYPES)
);

const holdOverGame = makeGame();
holdOverGame.start();
holdOverGame.hold(); /* O -> hold, T spawns */
lockCurrent(holdOverGame); /* lock T -> S spawns, hold re-enabled */
fillRow(holdOverGame.board, 0);
fillRow(holdOverGame.board, 1);
check('hold with a blocked spawn enters GAME_OVER', holdOverGame.hold() === true &&
  holdOverGame.phase === VT.Game.Phase.GAME_OVER);
check('the swap still moved the current piece into hold', holdOverGame.heldType === 'S');
check('hold refused after game over', holdOverGame.hold() === false);


/* --- game.js: lock delay (FR-10) ----------------------------------------- */

function groundPiece(game) {
  while (game.canMoveDown()) {
    game.currentPiece.row += 1;
  }
}

const lockGame = makeGame();
lockGame.start(); /* seeded O piece */
groundPiece(lockGame);
check('canMoveDown is false once the piece rests', lockGame.canMoveDown() === false);
runMs(lockGame, VT.Game.LOCK_DELAY_MS - 1);
check(
  'a grounded piece survives just under the lock delay',
  lockGame.phase === VT.Game.Phase.FALLING && lockGame.currentPiece.type === 'O'
);
runMs(lockGame, 1);
check(
  'the piece locks LOCK_DELAY_MS after resting (FR-10)',
  lockGame.currentPiece.type === 'T' && lockGame.board.cells[19][4] === 'O'
);

const resetGame = makeGame();
resetGame.start();
groundPiece(resetGame);
runMs(resetGame, 400);
check(
  'a move while grounded resets the lock countdown (FR-10)',
  resetGame.moveLeft() &&
    resetGame.lockElapsedMs === 0 &&
    resetGame.lockResetsUsed === 1
);
runMs(resetGame, 400);
check('the piece survives 400 ms after a reset', resetGame.currentPiece.type === 'O');
runMs(resetGame, 100);
check('it still locks 500 ms after the reset', resetGame.currentPiece.type === 'T');

const capGame = makeGame();
capGame.start();
groundPiece(capGame);
runMs(capGame, 400);
let acceptedResets = 0;
for (let i = 0; i < VT.Game.MAX_LOCK_RESETS; i += 1) {
  /* alternate so every move succeeds on the open floor */
  if (i % 2 === 0 ? capGame.moveLeft() : capGame.moveRight()) {
    acceptedResets += 1;
  }
  runMs(capGame, 400);
}
check(
  '15 move resets are honoured',
  acceptedResets === VT.Game.MAX_LOCK_RESETS &&
    capGame.lockResetsUsed === VT.Game.MAX_LOCK_RESETS &&
    capGame.currentPiece.type === 'O'
);
capGame.moveLeft(); /* 16th move succeeds but buys no reset */
runMs(capGame, 100);
check(
  'the 16th move cannot extend the lock delay past the cap (FR-10)',
  capGame.currentPiece.type !== 'O'
);

/* A move that puts the piece back in the air cancels the countdown: the
 * piece must get the full lock delay when it lands again. */
const ledgeGame = makeGame();
ledgeGame.start(); /* seeded O piece, cols 4-5 */
ledgeGame.board.cells[19][5] = 'Z'; /* 1-wide pedestal under the right half */
ledgeGame.currentPiece.row = 17; /* resting on the pedestal */
check('the piece rests on the pedestal', ledgeGame.canMoveDown() === false);
runMs(ledgeGame, 400);
check('the grounded countdown accrues', ledgeGame.lockElapsedMs === 400);
check(
  'moving off the ledge puts the piece back in the air',
  ledgeGame.moveLeft() && ledgeGame.canMoveDown() === true
);
check(
  'leaving the ground cancels the countdown without spending a reset (FR-10)',
  ledgeGame.lockElapsedMs === 0 && ledgeGame.lockResetsUsed === 0
);
runMs(ledgeGame, VT.Game.gravityIntervalMs(1));
check(
  'the piece falls on and lands',
  ledgeGame.currentPiece.row === 18 &&
    ledgeGame.canMoveDown() === false &&
    ledgeGame.lockElapsedMs === 0
);
runMs(ledgeGame, VT.Game.LOCK_DELAY_MS - 1);
check(
  'the re-landed piece survives just under the full lock delay',
  ledgeGame.currentPiece.type === 'O'
);
runMs(ledgeGame, 1);
check(
  'the re-landed piece locks a full lock delay after landing (FR-10)',
  ledgeGame.currentPiece.type === 'T' && ledgeGame.board.cells[18][3] === 'O'
);

/* Same rule for a rotation that un-grounds the piece. */
const rotateAirGame = makeGame();
rotateAirGame.start();
rotateAirGame.board.cells[19][6] = 'Z';
rotateAirGame.currentPiece = new VT.Pieces.Piece('T', 4, 17, 3);
check('the rotating piece rests on the pedestal', rotateAirGame.canMoveDown() === false);
runMs(rotateAirGame, 400);
check(
  'a rotation into the air cancels the countdown (FR-10)',
  rotateAirGame.rotateCW() &&
    rotateAirGame.canMoveDown() === true &&
    rotateAirGame.lockElapsedMs === 0
);

/* --- game.js: drop scoring (FR-11) --------------------------------------- */

const hardGame = makeGame();
hardGame.start(); /* seeded O piece at row 0 */
const hardCells = hardGame.ghostPiece().row - hardGame.currentPiece.row;
check('hardDrop locks instantly (FR-10)', hardGame.hardDrop());
check(
  'hard drop scores +2 per cell fallen',
  hardGame.score === hardCells * VT.Game.HARD_DROP_SCORE_PER_CELL
);
check(
  'hard drop locked the piece at the ghost row',
  hardGame.board.cells[19][4] === 'O' && hardGame.currentPiece.type === 'T'
);
hardGame.level = 3;
const hardCells2 = hardGame.ghostPiece().row - hardGame.currentPiece.row;
const scoreBeforeHard = hardGame.score;
hardGame.hardDrop();
check(
  'the hard-drop bonus is not level-multiplied (FR-11)',
  hardGame.score - scoreBeforeHard === hardCells2 * VT.Game.HARD_DROP_SCORE_PER_CELL
);

const softGame = makeGame();
softGame.start();
const softRow = softGame.currentPiece.row;
softGame.softDrop();
check(
  'a soft-drop press moves 1 cell and scores +1 (FR-11)',
  softGame.currentPiece.row === softRow + 1 &&
    softGame.score === 1 &&
    softGame.softDropDescentCells === 1
);
softGame.setSoftDropHeld(true);
runMs(softGame, VT.Game.SOFT_DROP_GRAVITY_MS);
check(
  'held Down accelerates gravity and keeps scoring +1/cell',
  softGame.currentPiece.row === softRow + 2 && softGame.score === 2
);
softGame.setSoftDropHeld(false);
check(
  'release resets the descent accumulator (FR-11)',
  softGame.softDropDescentCells === 0
);
const scoreAfterRelease = softGame.score;
runMs(softGame, VT.Game.gravityIntervalMs(1));
check(
  'gravity after release falls at the normal rate without soft score',
  softGame.currentPiece.row === softRow + 3 && softGame.score === scoreAfterRelease
);
softGame.setSoftDropHeld(true);
runMs(softGame, VT.Game.SOFT_DROP_GRAVITY_MS);
softGame.hardDrop();
check(
  'locking resets the soft-drop accumulator',
  softGame.softDropDescentCells === 0 && softGame.softDropHeld === false
);

/* --- game.js: pause (EC-03 / EC-08) -------------------------------------- */

const pauseGame = makeGame();
check('pause refused before start', pauseGame.togglePause() === false);
pauseGame.start();
const pauseRow = pauseGame.currentPiece.row;
check('togglePause pauses', pauseGame.togglePause() && pauseGame.paused === true);
runMs(pauseGame, 5000);
check('a paused game does not fall (EC-03)', pauseGame.currentPiece.row === pauseRow);
check(
  'gameplay actions are refused while paused',
  pauseGame.moveLeft() === false &&
    pauseGame.moveRight() === false &&
    pauseGame.rotateCW() === false &&
    pauseGame.rotateCCW() === false &&
    pauseGame.softDrop() === false &&
    pauseGame.hardDrop() === false &&
    pauseGame.hold() === false
);
check('togglePause resumes', pauseGame.togglePause() && pauseGame.paused === false);
runMs(pauseGame, VT.Game.gravityIntervalMs(1));
check('gravity resumes cleanly after unpause', pauseGame.currentPiece.row === pauseRow + 1);

const blurGame = makeGame();
blurGame.start();
check('blur auto-pause pauses a falling game (EC-08)', blurGame.pause() && blurGame.paused === true);
const blurOverGame = makeGame();
blurOverGame.start();
fillRow(blurOverGame.board, 0);
fillRow(blurOverGame.board, 1);
blurOverGame.spawnNext();
check('blur auto-pause refused after game over', blurOverGame.pause() === false);

const clampGame = makeGame();
clampGame.start();
clampGame.update(60000); /* a backgrounded tab's huge rAF delta */
check(
  'a huge frame delta is clamped — no jump-drop (EC-08)',
  clampGame.currentPiece.row === 0 &&
    clampGame.gravityElapsedMs === VT.Game.MAX_TICK_MS
);

/* --- input.js ------------------------------------------------------------- */

function makeFakeElement() {
  return {
    handlers: {},
    addEventListener(type, fn) {
      this.handlers[type] = fn;
    },
    dispatch(event) {
      this.handlers.keydown(event);
    },
    dispatchKeyup(key) {
      this.handlers.keyup({ key, preventDefault() {} });
    },
    dispatchBlur() {
      this.handlers.blur();
    },
  };
}

let prevented = 0;
const fakeEvent = (key, repeat) => ({
  key,
  repeat: repeat === true,
  preventDefault() {
    prevented += 1;
  },
});

const inputCalls = [];
const heldCalls = [];
const fallingGame = {
  phase: VT.Game.Phase.FALLING,
  paused: false,
  moveLeft() { inputCalls.push('moveLeft'); },
  moveRight() { inputCalls.push('moveRight'); },
  softDrop() { inputCalls.push('softDrop'); },
  setSoftDropHeld(held) { heldCalls.push(held); },
  rotateCW() { inputCalls.push('rotateCW'); },
  rotateCCW() { inputCalls.push('rotateCCW'); },
  hardDrop() { inputCalls.push('hardDrop'); },
  togglePause() { inputCalls.push('togglePause'); },
  pause() { inputCalls.push('pause'); },
  start() { inputCalls.push('start'); return false; },
};
const fallingElement = makeFakeElement();
VT.Input.attachKeyboard(fallingGame, fallingElement);
fallingElement.dispatch(fakeEvent('ArrowLeft'));
fallingElement.dispatch(fakeEvent('ArrowRight'));
fallingElement.dispatch(fakeEvent('ArrowDown'));
fallingElement.dispatch(fakeEvent('ArrowUp'));
fallingElement.dispatch(fakeEvent('x'));
fallingElement.dispatch(fakeEvent('X'));
fallingElement.dispatch(fakeEvent('z'));
fallingElement.dispatch(fakeEvent('Z'));
fallingElement.dispatch(fakeEvent('Control'));
fallingElement.dispatch(fakeEvent(' '));
fallingElement.dispatch(fakeEvent('Backspace'));
fallingElement.dispatch(fakeEvent('r'));
check(
  'FR-05 keys map to game actions incl. rotations and hard drop',
  inputCalls.join(',') ===
    'moveLeft,moveRight,softDrop,rotateCW,rotateCW,rotateCW,' +
    'rotateCCW,rotateCCW,rotateCCW,hardDrop,start,start'
);
check('Down keydown arms held soft drop (FR-05)', heldCalls.join(',') === 'true');
fallingElement.dispatchKeyup('ArrowDown');
check('Down keyup disarms held soft drop', heldCalls.join(',') === 'true,false');
check('mapped keys are consumed', prevented === 12);

/* OS auto-repeat events are ignored; the DAS repeater owns repeats. */
const movesBefore = inputCalls.filter((c) => c === 'moveLeft').length;
fallingElement.dispatch(fakeEvent('ArrowLeft', true));
check(
  'event.repeat keydowns are suppressed (FR-06)',
  inputCalls.filter((c) => c === 'moveLeft').length === movesBefore
);

const readyCalls = [];
const readyFake = {
  phase: VT.Game.Phase.READY,
  start() { readyCalls.push('start'); return true; },
  moveLeft() { readyCalls.push('moveLeft'); },
};
const readyElement = makeFakeElement();
VT.Input.attachKeyboard(readyFake, readyElement);
readyElement.dispatch(fakeEvent('x'));
readyElement.dispatch(fakeEvent('ArrowLeft'));
check('any keypress starts the game from READY', readyCalls.join(',') === 'start,start');
check('the starting key is not replayed as gameplay', !readyCalls.includes('moveLeft'));
check('READY keypresses are consumed', prevented === 15); /* +1: the suppressed repeat keydown is still consumed */

const holdInputCalls = [];
const holdFakeGame = {
  phase: VT.Game.Phase.FALLING,
  paused: false,
  hold() { holdInputCalls.push('hold'); return true; },
};
const holdInputElement = makeFakeElement();
VT.Input.attachKeyboard(holdFakeGame, holdInputElement);
const preventedBeforeHold = prevented;
holdInputElement.dispatch(fakeEvent('c'));
holdInputElement.dispatch(fakeEvent('C'));
holdInputElement.dispatch(fakeEvent('Shift'));
check(
  'C / Shift map to hold (FR-05)',
  holdInputCalls.join(',') === 'hold,hold,hold'
);
check('hold keys are consumed', prevented - preventedBeforeHold === 3);

/* EC-03: while paused only the pause toggle passes. */
const pauseInputCalls = [];
const pausedFake = {
  phase: VT.Game.Phase.FALLING,
  paused: true,
  moveLeft() { pauseInputCalls.push('moveLeft'); },
  softDrop() { pauseInputCalls.push('softDrop'); },
  hardDrop() { pauseInputCalls.push('hardDrop'); },
  hold() { pauseInputCalls.push('hold'); },
  togglePause() { pauseInputCalls.push('togglePause'); },
};
const pauseInputElement = makeFakeElement();
VT.Input.attachKeyboard(pausedFake, pauseInputElement);
pauseInputElement.dispatch(fakeEvent('ArrowLeft'));
pauseInputElement.dispatch(fakeEvent('ArrowDown'));
pauseInputElement.dispatch(fakeEvent(' '));
pauseInputElement.dispatch(fakeEvent('c'));
pauseInputElement.dispatch(fakeEvent('p'));
pauseInputElement.dispatch(fakeEvent('P'));
pauseInputElement.dispatch(fakeEvent('Escape'));
check(
  'paused: gameplay keys ignored, P/Escape still toggle pause (EC-03)',
  pauseInputCalls.join(',') === 'togglePause,togglePause,togglePause'
);

/* EC-08: window blur auto-pauses through game.pause(). */
const preventedBeforeBlur = prevented;
fallingElement.dispatchBlur();
check('blur triggers auto-pause (EC-08)', inputCalls[inputCalls.length - 1] === 'pause');
check('blur itself consumes no key', prevented === preventedBeforeBlur);

/* KeyRepeater: pure DAS cadence (FR-06). */
const repeater = new VT.Input.KeyRepeater(
  new VT.Input.RepeaterOptions(VT.Input.DAS_DELAY_MS, VT.Input.ARR_INTERVAL_MS)
);
check('no repeats without a held key', repeater.tick(1000) === 0);
repeater.press('moveLeft');
check('no repeats before the DAS delay', repeater.tick(VT.Input.DAS_DELAY_MS - 1) === 0);
let repeatFirings = 0;
for (let i = 0; i < 10; i += 1) {
  repeatFirings += repeater.tick(VT.Input.ARR_INTERVAL_MS);
}
check('held key repeats at the ARR cadence after DAS', repeatFirings === 10);
repeater.press('moveRight');
repeater.release('moveLeft');
check('releasing another action keeps the held one', repeater.heldAction === 'moveRight');
repeater.release('moveRight');
check('release stops the repeats', repeater.tick(1000) === 0);

/* attachKeyboard drives the repeater through the injected timer. */
const dasCalls = [];
const dasTimers = {
  setInterval(fn) {
    dasTimers.callback = fn;
    return 1;
  },
};
const dasGame = {
  phase: VT.Game.Phase.FALLING,
  paused: false,
  moveLeft() { dasCalls.push('moveLeft'); },
};
const dasElement = makeFakeElement();
VT.Input.attachKeyboard(dasGame, dasElement, dasTimers);
dasElement.dispatch(fakeEvent('ArrowLeft'));
check('held-key timer is installed', typeof dasTimers.callback === 'function');
for (let i = 0; i < 20; i += 1) {
  dasTimers.callback(); /* 20 x REPEAT_TICK_MS = 320 ms > DAS */
}
check('held Left auto-repeats past the DAS delay', dasCalls.length > 1);
const dasCount = dasCalls.length;
dasElement.dispatchKeyup('ArrowLeft');
for (let i = 0; i < 20; i += 1) {
  dasTimers.callback();
}
check('keyup stops the auto-repeat', dasCalls.length === dasCount);

/* AC-10 / EC-08: blur swallows the keyup of a held key, so blur has to
 * drop the DAS repeater and the held soft drop. */
const blurResumeCalls = [];
const blurResumeHeld = [];
const blurResumeTimers = {
  setInterval(fn) {
    blurResumeTimers.callback = fn;
    return 1;
  },
  clearInterval() {
    blurResumeTimers.cleared = (blurResumeTimers.cleared || 0) + 1;
  },
};
const blurResumeGame = {
  phase: VT.Game.Phase.FALLING,
  paused: false,
  moveLeft() { blurResumeCalls.push('moveLeft'); },
  softDrop() { blurResumeCalls.push('softDrop'); },
  setSoftDropHeld(held) { blurResumeHeld.push(held); },
  pause() { this.paused = true; return true; },
  togglePause() { this.paused = !this.paused; return true; },
};
const blurResumeElement = makeFakeElement();
VT.Input.attachKeyboard(blurResumeGame, blurResumeElement, blurResumeTimers);
blurResumeElement.dispatch(fakeEvent('ArrowLeft'));
blurResumeElement.dispatch(fakeEvent('ArrowDown'));
blurResumeElement.dispatchBlur();
check(
  'blur disarms the held soft drop (AC-10)',
  blurResumeHeld.join(',') === 'true,false'
);
check('blur clears the repeat timer', blurResumeTimers.cleared > 0);
blurResumeElement.dispatch(fakeEvent('p')); /* resume with no key held */
for (let i = 0; i < 20; i += 1) {
  blurResumeTimers.callback();
}
check(
  'no phantom auto-repeat after blur + resume (AC-10 / EC-08)',
  blurResumeCalls.filter((c) => c === 'moveLeft').length === 1
);

/* Same rule for a deliberate P pause taken while a key is repeating. */
const pauseHeldCalls = [];
const pauseHeldHeld = [];
const pauseHeldTimers = {
  setInterval(fn) {
    pauseHeldTimers.callback = fn;
    return 1;
  },
  clearInterval() {},
};
const pauseHeldGame = {
  phase: VT.Game.Phase.FALLING,
  paused: false,
  moveLeft() { pauseHeldCalls.push('moveLeft'); },
  setSoftDropHeld(held) { pauseHeldHeld.push(held); },
  togglePause() { this.paused = !this.paused; return true; },
};
const pauseHeldElement = makeFakeElement();
VT.Input.attachKeyboard(pauseHeldGame, pauseHeldElement, pauseHeldTimers);
pauseHeldElement.dispatch(fakeEvent('ArrowLeft'));
for (let i = 0; i < 20; i += 1) {
  pauseHeldTimers.callback(); /* 320 ms: past DAS, repeats are running */
}
const firingsBeforePause = pauseHeldCalls.length;
check('held Left repeats before the pause', firingsBeforePause > 1);
pauseHeldElement.dispatch(fakeEvent('p'));
check(
  'pausing disarms the held soft drop (AC-10)',
  pauseHeldHeld.join(',') === 'false'
);
for (let i = 0; i < 20; i += 1) {
  pauseHeldTimers.callback();
}
check('no repeats fire while paused (EC-03)', pauseHeldCalls.length === firingsBeforePause);
pauseHeldElement.dispatch(fakeEvent('p'));
for (let i = 0; i < 20; i += 1) {
  pauseHeldTimers.callback();
}
check(
  'resume does not restart a phantom repeat (AC-10)',
  pauseHeldCalls.length === firingsBeforePause
);

/* FR-19 / AC-07 / EC-04: with a real game in the exploding stage, every
 * FR-05 key — including R/Backspace/Space — is inert. */
const overInputGame = makeGame();
overInputGame.start();
fillRow(overInputGame.board, 0);
fillRow(overInputGame.board, 1);
overInputGame.spawnNext();
const overInputElement = makeFakeElement();
VT.Input.attachKeyboard(overInputGame, overInputElement);
[
  'ArrowLeft', 'ArrowRight', 'ArrowDown', 'ArrowUp', 'x', 'z', ' ',
  'c', 'p', 'Escape', 'r', 'Backspace',
].forEach((key) => overInputElement.dispatch(fakeEvent(key)));
check(
  'FR-19/EC-04: no key affects the game after game over',
  overInputGame.phase === VT.Game.Phase.GAME_OVER &&
    overInputGame.gameOverStage === VT.Game.GameOverStage.EXPLODING &&
    overInputGame.currentPiece === null &&
    overInputGame.paused === false &&
    overInputGame.start() === false
);

/* =========================================================================
 * Slice 8: "New Game" redirect, taunt visibility, canvas rescale, AC-08.
 * ========================================================================= */

/* --- the taunt screen rule (game.showsTaunt) --------------------------- */

function makeGameOverGame() {
  const game = makeGame();
  game.start();
  fillRow(game.board, 0);
  fillRow(game.board, 1);
  game.spawnNext(); /* block-out */
  return game;
}

const tauntGame = makeGameOverGame();
check(
  'taunt is hidden while the blast has barely started',
  tauntGame.explosion.progress() < VT.Game.TAUNT_REVEAL_PROGRESS &&
    tauntGame.showsTaunt() === false
);
runMs(tauntGame, VT.Explosion.DURATION_MS * VT.Game.TAUNT_REVEAL_PROGRESS);
check(
  'taunt appears during the explosion, once readable',
  tauntGame.showsTaunt() === true
);
runMs(tauntGame, VT.Explosion.DURATION_MS);
check(
  'taunt stays up for good in the TAUNT stage',
  tauntGame.gameOverStage === VT.Game.GameOverStage.TAUNT &&
    tauntGame.showsTaunt() === true
);
const tauntFalling = makeGame();
tauntFalling.start();
check(
  'no taunt during play',
  tauntFalling.showsTaunt() === false
);

/* --- the New Game button ---------------------------------------------- */

function makeRecordingNavigate() {
  return {
    urls: [],
    go(url) {
      this.urls.push(url);
    },
  };
}

function makeFakeButton() {
  return {
    hidden: true,
    focusCount: 0,
    handlers: {},
    addEventListener(type, fn) {
      this.handlers[type] = fn;
    },
    dispatch(type, event) {
      if (this.handlers[type]) {
        this.handlers[type](event);
      }
    },
    focus() {
      this.focusCount += 1;
    },
  };
}

function buttonEvent(key) {
  return { key, prevented: false, preventDefault() { this.prevented = true; } };
}

check(
  'FR-18 redirect target is meatspin.com',
  VT.NewGameButton.REDIRECT_URL === 'https://meatspin.com'
);

const earlyNavigate = makeRecordingNavigate();
const earlyButton = new VT.NewGameButton.NewGameButton(
  new VT.NewGameButton.NewGameButtonOptions(earlyNavigate)
);
const liveGame = makeGame();
liveGame.start();
check(
  'FR-19: activating New Game mid-game does nothing',
  earlyButton.activate(liveGame) === false && earlyNavigate.urls.length === 0
);

const overNavigate = makeRecordingNavigate();
const overButtonElement = makeFakeButton();
const overButtonGame = makeGameOverGame();
const overButton = VT.NewGameButton.attachNewGameButton(
  overButtonGame,
  overButtonElement,
  new VT.NewGameButton.NewGameButton(
    new VT.NewGameButton.NewGameButtonOptions(overNavigate)
  )
);
check('the button starts hidden', overButtonElement.hidden === true);
overButtonElement.dispatch('click', buttonEvent('click'));
check(
  'FR-19: clicking before the taunt appears does not navigate',
  overNavigate.urls.length === 0
);
runMs(overButtonGame, VT.Explosion.DURATION_MS * VT.Game.TAUNT_REVEAL_PROGRESS);
overButton.update(overButtonGame, overButtonElement);
check(
  'FR-18: the button appears with the taunt',
  overButtonElement.hidden === false
);
check(
  'the button is focused as it appears (keyboard path works)',
  overButtonElement.focusCount === 1
);
overButton.update(overButtonGame, overButtonElement);
check(
  'visibility sync is idempotent (focus is not stolen every frame)',
  overButtonElement.focusCount === 1
);
overButtonElement.dispatch('click', buttonEvent('click'));
check(
  'AC-06: clicking New Game navigates to meatspin.com',
  overNavigate.urls.join(',') === 'https://meatspin.com'
);
const enterEvent = buttonEvent('Enter');
overButtonElement.dispatch('keydown', enterEvent);
check(
  'FR-18: Enter on the focused button activates the redirect',
  overNavigate.urls.length === 2 && enterEvent.prevented === true
);
const spaceEvent = buttonEvent(' ');
overButtonElement.dispatch('keydown', spaceEvent);
check(
  'FR-18: Space on the focused button activates the redirect',
  overNavigate.urls.length === 3 && spaceEvent.prevented === true
);
const letterEvent = buttonEvent('a');
overButtonElement.dispatch('keydown', letterEvent);
check(
  'other keys are left to the browser',
  overNavigate.urls.length === 3 && letterEvent.prevented === false
);
/* Let the blast finish so the game is in its final TAUNT stage: activation
 * must not move it out of game over. */
runMs(overButtonGame, VT.Explosion.DURATION_MS);
check(
  'AC-07/AC-11: activation leaves the game over, it never restarts',
  overButtonGame.phase === VT.Game.Phase.GAME_OVER &&
    overButtonGame.gameOverStage === VT.Game.GameOverStage.TAUNT &&
    overButtonGame.currentPiece === null &&
    overButtonGame.score === 0
);

/* input.js hands Enter/Space to the focused button during game over. */
const handoffGame = makeGameOverGame();
const handoffElement = makeFakeElement();
VT.Input.attachKeyboard(handoffGame, handoffElement);
const handedEnter = { key: 'Enter', prevented: false, preventDefault() { this.prevented = true; } };
const handedSpace = { key: ' ', prevented: false, preventDefault() { this.prevented = true; } };
const handedArrow = { key: 'ArrowLeft', prevented: false, preventDefault() { this.prevented = true; } };
handoffElement.dispatch(handedEnter);
handoffElement.dispatch(handedSpace);
handoffElement.dispatch(handedArrow);
check(
  'FR-18: Enter/Space are not swallowed by gameplay input after game over',
  handedEnter.prevented === false && handedSpace.prevented === false
);
check(
  'EC-04: other keys stay consumed and inert after game over',
  handedArrow.prevented === true &&
    handoffGame.phase === VT.Game.Phase.GAME_OVER
);

/* --- canvas rescale on resize (EC-10) --------------------------------- */

function makeFakeCanvas(cssWidth, cssHeight) {
  const canvas = {
    clientWidth: cssWidth,
    clientHeight: cssHeight,
    width: 0,
    height: 0,
    transforms: [],
    getContext() {
      return {
        canvas,
        setTransform(a, b, c, d, e, f) {
          canvas.transforms.push([a, b, c, d, e, f]);
        },
      };
    },
  };
  return canvas;
}

MODULE_CONTEXT.devicePixelRatio = 1;
const resizeCanvas = makeFakeCanvas(300, 600);
const resizeGame = makeGame();
resizeGame.start();
resizeGame.board.cells[19][0] = 'T';
VT.Render.fitCanvas(resizeCanvas, VT.Render.playfieldSize());
check(
  'canvas backing store matches its CSS box at dpr 1',
  resizeCanvas.width === 300 && resizeCanvas.height === 600
);
check(
  'logical drawing space maps 1:1 at full size',
  resizeCanvas.transforms[0].join(',') === '1,0,0,1,0,0'
);
resizeCanvas.clientWidth = 150;
resizeCanvas.clientHeight = 300;
VT.Render.fitCanvas(resizeCanvas, VT.Render.playfieldSize());
check(
  'resize rescales the backing store',
  resizeCanvas.width === 150 && resizeCanvas.height === 300
);
check(
  'resize rescales the drawing transform',
  resizeCanvas.transforms[1].join(',') === '0.5,0,0,0.5,0,0'
);
check(
  'EC-10: game state survives the rescale',
  resizeGame.board.cells[19][0] === 'T' &&
    resizeGame.phase === VT.Game.Phase.FALLING &&
    resizeGame.currentPiece !== null
);
MODULE_CONTEXT.devicePixelRatio = 2;
const hidpiCanvas = makeFakeCanvas(300, 600);
VT.Render.fitCanvas(hidpiCanvas, VT.Render.playfieldSize());
check(
  'HiDPI backing store is CSS size x devicePixelRatio',
  hidpiCanvas.width === 600 && hidpiCanvas.height === 1200
);
check(
  'HiDPI transform keeps crisp scaling',
  hidpiCanvas.transforms[0].join(',') === '2,0,0,2,0,0'
);
MODULE_CONTEXT.devicePixelRatio = 1;

/* --- AC-08 / NFR-04: no assets, no network requests ------------------- */

const DELIVERABLE_EXTENSIONS = new Set(['.html', '.css', '.js', '.md', '.json']);
const FORBIDDEN_PATTERNS = [
  [/<img\b/i, 'image element'],
  [/<audio\b|<video\b|<source\b/i, 'media element'],
  [/<iframe\b/i, 'iframe'],
  [/@font-face|@import/i, 'external font or import'],
  [/url\(/i, 'css url()'],
  [/\.(?:png|jpe?g|gif|webp|svg|ico|bmp|mp3|wav|ogg|m4a|woff2?|ttf|otf)\b/i, 'asset file'],
  [/\bfetch\s*\(/, 'fetch call'],
  [/XMLHttpRequest/, 'XHR'],
  [/new\s+Image\b|createImageBitmap|importScripts/, 'image/network API'],
  [/<script[^>]+src=["']https?:/i, 'remote script'],
];

function listDeliverableFiles(dir, prefix) {
  const found = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (entry.name === 'tests' || entry.name.startsWith('.')) {
      continue;
    }
    const relative = prefix ? prefix + '/' + entry.name : entry.name;
    if (entry.isDirectory()) {
      found.push(...listDeliverableFiles(path.join(dir, entry.name), relative));
    } else {
      found.push(relative);
    }
  }
  return found;
}

const deliverableFiles = listDeliverableFiles(APP_DIR, '');
check(
  'AC-08: deliverable holds only source files (no asset files)',
  deliverableFiles.every(
    (file) => DELIVERABLE_EXTENSIONS.has(path.extname(file))
  )
);

const assetViolations = [];
const networkViolations = [];
for (const file of deliverableFiles) {
  const text = fs.readFileSync(path.join(APP_DIR, file), 'utf8');
  for (const [pattern, label] of FORBIDDEN_PATTERNS) {
    if (pattern.test(text)) {
      assetViolations.push(file + ': ' + label);
    }
  }
  for (const url of text.match(/https?:\/\/[^\s"')<>]+/g) || []) {
    if (url !== VT.NewGameButton.REDIRECT_URL) {
      networkViolations.push(file + ': ' + url);
    }
  }
}
check(
  'AC-08/NFR-04: no assets, fonts or network APIs in the deliverable' +
    (assetViolations.length ? ' -> ' + assetViolations.join('; ') : ''),
  assetViolations.length === 0
);
check(
  'AC-08: the only URL in the deliverable is the game-over redirect' +
    (networkViolations.length ? ' -> ' + networkViolations.join('; ') : ''),
  networkViolations.length === 0
);

/* --- the page wires the button up ------------------------------------- */

const pageHtml = fs.readFileSync(path.join(APP_DIR, 'index.html'), 'utf8');
check(
  'index.html ships a focusable New Game button, hidden at boot',
  /<button[^>]*id=["']new-game["'][^>]*hidden[^>]*>/.test(pageHtml) &&
    /New Game/.test(pageHtml)
);
check(
  'index.html loads the New Game module before main.js',
  pageHtml.indexOf('js/new_game_button.js') < pageHtml.indexOf('js/main.js')
);

console.log(failures === 0 ? '\nAll tests passed.' : '\n' + failures + ' failure(s).');
process.exit(failures === 0 ? 0 : 1);
