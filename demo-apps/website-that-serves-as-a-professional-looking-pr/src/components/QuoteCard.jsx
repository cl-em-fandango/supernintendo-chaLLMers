import React from 'react';

// QuoteCard.jsx — a single styled testimonial: quote text, attributed
// name, and affiliation line. `accent` selects a gold/pink/purple edge.
export default function QuoteCard({ testimonial }) {
  const accentClass = testimonial.accent
    ? ` hml-quote--${testimonial.accent}`
    : '';
  return (
    <figure
      className={`hml-card hml-quote${accentClass}`}
      aria-label={`Testimonial from ${testimonial.name}`}
    >
      <blockquote className="hml-quote__text">{testimonial.quote}</blockquote>
      <figcaption className="hml-quote__caption">
        <span className="hml-quote__name">{testimonial.name}</span>
        <span className="hml-quote__affiliation">{testimonial.affiliation}</span>
      </figcaption>
    </figure>
  );
}
