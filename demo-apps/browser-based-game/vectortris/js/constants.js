/* constants.js — app-wide namespace, board dimensions, and theme colors.
 *
 * No behavior lives here. Plain <script> files (no ES modules) so the page
 * works from file:// (spec §3 / AC-01); this file creates the single `VT`
 * namespace every other module attaches to (NFR-05: no global-variable soup).
 */
(function (global) {
  'use strict';

  var VT = {};

  /* Board dimensions moved to js/board.js (slice 2): the grid-state module
   * owns the shape of the grid it stores.
   *
   * Dark-theme palette (FR-22). */
  VT.Theme = Object.freeze({
    PAGE_BACKGROUND: '#0d1117',
    PLAYFIELD_BACKGROUND: '#10151c',
    GRID_LINE: '#2a313a',
    PLAYFIELD_BORDER: '#8b949e',
    PANEL_BACKGROUND: '#141a22',
    TEXT_PRIMARY: '#e6edf3',
    TAUNT: '#ff1744', /* FR-17 "YOU SUCK" red */
  });

  /* FR-02 standard tetromino colors. Unused in slice 1; the piece, render
   * and explosion modules of later slices read colors from here. */
  VT.TetrominoColors = Object.freeze({
    I: '#00bcd4', /* cyan    */
    O: '#ffd600', /* yellow  */
    T: '#aa00ff', /* purple  */
    S: '#00e676', /* green   */
    Z: '#ff1744', /* red     */
    J: '#2979ff', /* blue    */
    L: '#ff9100', /* orange  */
  });

  global.VT = VT;
})(typeof window !== 'undefined' ? window : globalThis);
