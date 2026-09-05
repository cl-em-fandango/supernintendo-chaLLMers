import React from 'react';
import { Link } from 'react-router-dom';
import {
  TESTIMONIALS_INTRO,
  REAL_TESTIMONIALS,
  FICTIONAL_TESTIMONIALS,
  TESTIMONIALS_OUTRO,
} from '../testimonialsContent';
import QuoteCard from '../components/QuoteCard';
import '../testimonials.css';

// Testimonials.jsx — composition of the Testimonials route: intro, the
// three mandated real-person quotes (verbatim, spec §6), a wall of
// clearly fictional user quotes, and an outro CTA.
export default function Testimonials() {
  return (
    <>
      <section className="hml-section" aria-labelledby="testimonials-title">
        <h1 id="testimonials-title">{TESTIMONIALS_INTRO.heading}</h1>
        <p className="hml-testimonials__deck">{TESTIMONIALS_INTRO.deck}</p>
      </section>

      <section className="hml-section" aria-labelledby="headliners-title">
        <h2 id="headliners-title" className="hml-testimonials__wall-title">
          <span className="hml-testimonials__sparkle" aria-hidden="true">
            ✦
          </span>
          Headliner Endorsements
        </h2>
        <div className="hml-grid">
          {REAL_TESTIMONIALS.map((testimonial) => (
            <QuoteCard
              key={testimonial.id}
              testimonial={testimonial}
            />
          ))}
        </div>
      </section>

      <section className="hml-section" aria-labelledby="users-title">
        <h2 id="users-title" className="hml-testimonials__wall-title">
          <span className="hml-testimonials__sparkle" aria-hidden="true">
            ✦
          </span>
          From the User Base
        </h2>
        <div className="hml-grid">
          {FICTIONAL_TESTIMONIALS.map((testimonial) => (
            <QuoteCard
              key={testimonial.id}
              testimonial={testimonial}
            />
          ))}
        </div>
      </section>

      <section
        className="hml-card hml-testimonial-outro"
        aria-labelledby="testimonials-outro-title"
      >
        <h2 id="testimonials-outro-title">{TESTIMONIALS_OUTRO.heading}</h2>
        <p>{TESTIMONIALS_OUTRO.body}</p>
        <div className="hml-testimonial-outro__cta">
          <Link
            className="hml-button hml-button--primary"
            to={TESTIMONIALS_OUTRO.primaryCta.to}
          >
            {TESTIMONIALS_OUTRO.primaryCta.label}
          </Link>
          <Link
            className="hml-button hml-button--secondary"
            to={TESTIMONIALS_OUTRO.secondaryCta.to}
          >
            {TESTIMONIALS_OUTRO.secondaryCta.label}
          </Link>
        </div>
      </section>
    </>
  );
}
