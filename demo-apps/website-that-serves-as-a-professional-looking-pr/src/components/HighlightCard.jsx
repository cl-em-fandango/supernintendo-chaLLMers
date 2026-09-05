import React from 'react';
import { Link } from 'react-router-dom';

// HighlightCard.jsx — one feature-teaser card on the Home view.
// Cards link to /features where the feature gets its full treatment.
export default function HighlightCard({ highlight }) {
  return (
    <article className="hml-card hml-highlight">
      <span className="hml-highlight__icon" aria-hidden="true">
        {highlight.icon}
      </span>
      <h3>{highlight.title}</h3>
      <p>{highlight.blurb}</p>
      <Link className="hml-highlight__link" to="/features">
        Explore the feature →
      </Link>
    </article>
  );
}
