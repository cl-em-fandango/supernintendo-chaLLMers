import React from 'react';
import { Link } from 'react-router-dom';
import { FEATURES, FEATURES_INTRO, FEATURES_OUTRO } from '../featuresContent';
import FeatureSection from '../components/FeatureSection';
import '../features.css';

// Features.jsx — composition of the Features route: intro, the mandated
// feature subsections (dual-persona kernel, hybrid, display manager,
// security module, plus the privacy-mode bonus), and an outro CTA.
export default function Features() {
  return (
    <>
      <section className="hml-section" aria-labelledby="features-intro-title">
        <h1 id="features-intro-title">{FEATURES_INTRO.heading}</h1>
        <p className="hml-feature__paragraph">{FEATURES_INTRO.deck}</p>
      </section>
      {FEATURES.map((feature) => (
        <FeatureSection key={feature.id} feature={feature} />
      ))}
      <section
        className="hml-card hml-feature-outro"
        aria-labelledby="features-outro-title"
      >
        <h2 id="features-outro-title">{FEATURES_OUTRO.heading}</h2>
        <p>{FEATURES_OUTRO.body}</p>
        <div className="hml-feature-outro__cta">
          <Link
            className="hml-button hml-button--primary"
            to={FEATURES_OUTRO.primaryCta.to}
          >
            {FEATURES_OUTRO.primaryCta.label}
          </Link>
          <Link
            className="hml-button hml-button--secondary"
            to={FEATURES_OUTRO.secondaryCta.to}
          >
            {FEATURES_OUTRO.secondaryCta.label}
          </Link>
        </div>
      </section>
    </>
  );
}
