/* rotation.js — SRS rotation: the four rotation states of every tetromino
 * and the Super Rotation System wall-kick tables (FR-07 / EC-01 / EC-02).
 *
 * Pure data + pure functions over a piece and a board; the phase machine
 * and input mapping live in game.js / input.js.
 *
 * Rotation states are indexed 0..3 (0 = standard spawn orientation,
 * 1 = R after one CW turn, 2 = 180, 3 = L after one CCW turn). States 1-3
 * are derived once at load time by rotating the spawn cells around each
 * type's SRS rotation centre, so state 0 stays the single source of truth
 * for the shapes.
 *
 * Kick tables are the standard SRS tables translated to screen coordinates
 * (dRow positive = down; the usual tables are y-up). Each entry is tried in
 * order after the rotation is applied; the first offset where the board
 * accepts the piece wins, otherwise the rotation is rejected.
 */
(function (global) {
  'use strict';

  /* Rotation input — an enum object, not scattered numbers. */
  var Direction = Object.freeze({
    CW: 'CW',
    CCW: 'CCW',
  });

  /* Spawn-state (rotation state 0) cell offsets relative to the piece
   * origin. Standard spawn orientations (FR-02): 3-wide pieces (T S Z J L)
   * use a center-relative origin, I spans 4 columns, O is a 2x2 block. */
  var SPAWN_SHAPES = Object.freeze({
    I: Object.freeze([
      Object.freeze({ col: -1, row: 0 }),
      Object.freeze({ col: 0, row: 0 }),
      Object.freeze({ col: 1, row: 0 }),
      Object.freeze({ col: 2, row: 0 }),
    ]),
    O: Object.freeze([
      Object.freeze({ col: 0, row: 0 }),
      Object.freeze({ col: 1, row: 0 }),
      Object.freeze({ col: 0, row: 1 }),
      Object.freeze({ col: 1, row: 1 }),
    ]),
    T: Object.freeze([
      Object.freeze({ col: 0, row: 0 }),
      Object.freeze({ col: -1, row: 1 }),
      Object.freeze({ col: 0, row: 1 }),
      Object.freeze({ col: 1, row: 1 }),
    ]),
    S: Object.freeze([
      Object.freeze({ col: 0, row: 0 }),
      Object.freeze({ col: 1, row: 0 }),
      Object.freeze({ col: -1, row: 1 }),
      Object.freeze({ col: 0, row: 1 }),
    ]),
    Z: Object.freeze([
      Object.freeze({ col: -1, row: 0 }),
      Object.freeze({ col: 0, row: 0 }),
      Object.freeze({ col: 0, row: 1 }),
      Object.freeze({ col: 1, row: 1 }),
    ]),
    J: Object.freeze([
      Object.freeze({ col: -1, row: 0 }),
      Object.freeze({ col: -1, row: 1 }),
      Object.freeze({ col: 0, row: 1 }),
      Object.freeze({ col: 1, row: 1 }),
    ]),
    L: Object.freeze([
      Object.freeze({ col: 1, row: 0 }),
      Object.freeze({ col: -1, row: 1 }),
      Object.freeze({ col: 0, row: 1 }),
      Object.freeze({ col: 1, row: 1 }),
    ]),
  });

  /* SRS rotation centres relative to the piece origin. JLSTZ rotate about
   * the centre cell of their 3x3 box; I rotates about the centre point of
   * its 4x4 box (a corner between cells, hence half-integers); O never
   * rotates. */
  var ROTATION_CENTRES = Object.freeze({
    I: Object.freeze({ col: 0.5, row: 0.5 }),
    O: null,
    T: Object.freeze({ col: 0, row: 1 }),
    S: Object.freeze({ col: 0, row: 1 }),
    Z: Object.freeze({ col: 0, row: 1 }),
    J: Object.freeze({ col: 0, row: 1 }),
    L: Object.freeze({ col: 0, row: 1 }),
  });

  /* One clockwise quarter-turn in screen coordinates (row axis points
   * down): (col, row) -> (-row, col). */
  function rotateCellCW(cell) {
    return { col: -cell.row, row: cell.col };
  }

  /* All four rotation states for one type: state 0 is the spawn shape,
   * states 1..3 are successive CW turns about the type's centre. O's four
   * states are identical (FR-07: the O-piece does not shift). */
  function deriveStates(type) {
    var spawn = SPAWN_SHAPES[type];
    var centre = ROTATION_CENTRES[type];
    if (centre === null) {
      return Object.freeze([spawn, spawn, spawn, spawn]);
    }
    var states = [spawn];
    var cells = spawn;
    var turn;
    for (turn = 1; turn < 4; turn += 1) {
      cells = cells.map(function (cell) {
        var rel = { col: cell.col - centre.col, row: cell.row - centre.row };
        var rotated = rotateCellCW(rel);
        return Object.freeze({
          col: Math.round(rotated.col + centre.col),
          row: Math.round(rotated.row + centre.row),
        });
      });
      states.push(Object.freeze(cells));
    }
    return Object.freeze(states);
  }

  var STATES = Object.freeze({
    I: deriveStates('I'),
    O: deriveStates('O'),
    T: deriveStates('T'),
    S: deriveStates('S'),
    Z: deriveStates('Z'),
    J: deriveStates('J'),
    L: deriveStates('L'),
  });

  function kick(dCol, dRow) {
    return Object.freeze({ dCol: dCol, dRow: dRow });
  }

  /* Standard SRS kick table for J, L, S, T and Z, in screen coordinates
   * (dRow positive = down), keyed "fromState>toState". */
  var KICKS_JLSTZ = Object.freeze({
    '0>1': Object.freeze([kick(0, 0), kick(-1, 0), kick(-1, -1), kick(0, 2), kick(-1, 2)]),
    '1>0': Object.freeze([kick(0, 0), kick(1, 0), kick(1, 1), kick(0, -2), kick(1, -2)]),
    '1>2': Object.freeze([kick(0, 0), kick(1, 0), kick(1, 1), kick(0, -2), kick(1, -2)]),
    '2>1': Object.freeze([kick(0, 0), kick(-1, 0), kick(-1, -1), kick(0, 2), kick(-1, 2)]),
    '2>3': Object.freeze([kick(0, 0), kick(1, 0), kick(1, -1), kick(0, 2), kick(1, 2)]),
    '3>2': Object.freeze([kick(0, 0), kick(-1, 0), kick(-1, 1), kick(0, -2), kick(-1, -2)]),
    '3>0': Object.freeze([kick(0, 0), kick(-1, 0), kick(-1, 1), kick(0, -2), kick(-1, -2)]),
    '0>3': Object.freeze([kick(0, 0), kick(1, 0), kick(1, -1), kick(0, 2), kick(1, 2)]),
  });

  /* Standard SRS kick table for the I-piece (EC-02: its own table), in
   * screen coordinates. */
  var KICKS_I = Object.freeze({
    '0>1': Object.freeze([kick(0, 0), kick(-2, 0), kick(1, 0), kick(-2, 1), kick(1, -2)]),
    '1>0': Object.freeze([kick(0, 0), kick(2, 0), kick(-1, 0), kick(2, -1), kick(-1, 2)]),
    '1>2': Object.freeze([kick(0, 0), kick(-1, 0), kick(2, 0), kick(-1, -2), kick(2, 1)]),
    '2>1': Object.freeze([kick(0, 0), kick(1, 0), kick(-2, 0), kick(1, 2), kick(-2, -1)]),
    '2>3': Object.freeze([kick(0, 0), kick(2, 0), kick(-1, 0), kick(2, -1), kick(-1, 2)]),
    '3>2': Object.freeze([kick(0, 0), kick(-2, 0), kick(1, 0), kick(-2, 1), kick(1, -2)]),
    '3>0': Object.freeze([kick(0, 0), kick(1, 0), kick(-2, 0), kick(1, 2), kick(-2, -1)]),
    '0>3': Object.freeze([kick(0, 0), kick(-1, 0), kick(2, 0), kick(-1, -2), kick(2, 1)]),
  });

  /* O never shifts: its only "kick" is the identity offset. */
  var KICKS_O = Object.freeze({ identity: Object.freeze([kick(0, 0)]) });

  /* Cell offsets of `type` in rotation state `rotation`. */
  function cellsFor(type, rotation) {
    return STATES[type][rotation];
  }

  /* The ordered kick list for one rotation transition. */
  function kicksFor(type, fromRotation, toRotation) {
    if (type === 'O') {
      return KICKS_O.identity;
    }
    var table = type === 'I' ? KICKS_I : KICKS_JLSTZ;
    return table[fromRotation + '>' + toRotation];
  }

  function nextRotation(rotation, direction) {
    var delta = direction === Direction.CW ? 1 : -1;
    return ((rotation + delta) % 4 + 4) % 4;
  }

  /* Apply `direction`'s rotation to `piece`, trying every SRS kick against
   * `board`. The first offset that places the piece legally is kept and
   * true is returned; otherwise the piece is left byte-for-byte untouched
   * and false is returned (EC-01: never overlaps a filled cell or leaves
   * the field). */
  function tryRotate(piece, direction, board) {
    var fromRotation = piece.rotation;
    var toRotation = nextRotation(fromRotation, direction);
    var kicks = kicksFor(piece.type, fromRotation, toRotation);
    var i;
    for (i = 0; i < kicks.length; i += 1) {
      piece.rotation = toRotation;
      piece.col += kicks[i].dCol;
      piece.row += kicks[i].dRow;
      if (board.canPlace(piece)) {
        return true;
      }
      piece.col -= kicks[i].dCol;
      piece.row -= kicks[i].dRow;
    }
    piece.rotation = fromRotation;
    return false;
  }

  global.VT.Rotation = Object.freeze({
    Direction: Direction,
    SPAWN_SHAPES: SPAWN_SHAPES,
    STATES: STATES,
    KICKS_JLSTZ: KICKS_JLSTZ,
    KICKS_I: KICKS_I,
    cellsFor: cellsFor,
    kicksFor: kicksFor,
    nextRotation: nextRotation,
    tryRotate: tryRotate,
  });
})(typeof window !== 'undefined' ? window : globalThis);
