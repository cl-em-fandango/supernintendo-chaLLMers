import React from 'react';

// StageMascot.jsx — the decorative stage-penguin motif: a tux-adjacent
// penguin wearing a blonde wig, composed purely from emoji (no image
// assets, no copyrighted media). Entirely ornamental, so the wrapper is
// aria-hidden and carries no interactive behavior.
export default function StageMascot() {
  return (
    <span className="hml-mascot" aria-hidden="true">
      <span className="hml-mascot__wig">👱‍♀️</span>
      <span className="hml-mascot__penguin">🐧</span>
      <span className="hml-mascot__sparkles">✦ ✧ ✦</span>
    </span>
  );
}
