import React from 'react';
import { Link } from 'react-router-dom';
import content from '../content.json';
import { HERO, HIGHLIGHTS, STATS, TESTIMONIAL_TEASER } from '../homeContent';
import Hero from '../components/Hero';
import HighlightCard from '../components/HighlightCard';
import StatsStrip from '../components/StatsStrip';
import ContentSections from '../components/ContentSections';
import '../home.css';

// Home.jsx — composition of the Home route: hero, product overview
// (rendered from content.json), feature teasers, fake stats, and a
// pointer to the testimonial wall.
export default function Home() {
  return (
    <>
      <Hero hero={HERO} />
      <StatsStrip stats={STATS} />
      <section className="hml-section" aria-labelledby="home-overview-title">
        <h2 id="home-overview-title" className="hml-visually-hidden">
          Product overview
        </h2>
        <ContentSections sections={content.sections} />
      </section>
      <section className="hml-section" aria-labelledby="home-highlights-title">
        <h2 id="home-highlights-title">Engineered for both worlds</h2>
        <p>
          Four of the features our install base talks about most. The full
          engineering deep-dive — complete with terminal screenshots that
          never happened — lives on the features page.
        </p>
        <div className="hml-grid hml-highlights">
          {HIGHLIGHTS.map((highlight) => (
            <HighlightCard key={highlight.id} highlight={highlight} />
          ))}
        </div>
      </section>
      <section
        className="hml-card hml-teaser"
        aria-labelledby="home-teaser-title"
      >
        <h2 id="home-teaser-title">{TESTIMONIAL_TEASER.heading}</h2>
        <p>{TESTIMONIAL_TEASER.body}</p>
        <Link className="hml-button hml-button--primary" to={TESTIMONIAL_TEASER.to}>
          {TESTIMONIAL_TEASER.ctaLabel}
        </Link>
      </section>
    </>
  );
}
