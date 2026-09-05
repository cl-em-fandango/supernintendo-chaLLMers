import React from 'react';
import { Link } from 'react-router-dom';
import {
  PRICING_INTRO,
  PRICING_TIERS,
  PRICING_FAQ,
  PRICING_OUTRO,
} from '../pricingContent';
import PricingCard from '../components/PricingCard';
import FAQ from '../components/FAQ';
import '../pricing.css';

// Pricing.jsx — composition of the Pricing route: intro deck, three tier
// cards (Miley Basic, Hannah Pro, The Other One Enterprise), an FAQ block,
// and an outro CTA. All copy lives in pricingContent.js.
export default function Pricing() {
  return (
    <>
      <section className="hml-section" aria-labelledby="pricing-title">
        <h1 id="pricing-title">{PRICING_INTRO.heading}</h1>
        <p className="hml-pricing__deck">{PRICING_INTRO.deck}</p>
      </section>

      <section className="hml-section" aria-labelledby="tiers-title">
        <h2 id="tiers-title" className="hml-pricing__tiers-title">
          <span className="hml-pricing__sparkle" aria-hidden="true">
            ✦
          </span>
          Choose Your Persona
        </h2>
        <div className="hml-grid hml-pricing__grid">
          {PRICING_TIERS.map((tier) => (
            <PricingCard key={tier.id} tier={tier} />
          ))}
        </div>
      </section>

      <section className="hml-section" aria-labelledby="pricing-faq-title">
        <h2 id="pricing-faq-title" className="hml-pricing__tiers-title">
          <span className="hml-pricing__sparkle" aria-hidden="true">
            ✦
          </span>
          {PRICING_FAQ.heading}
        </h2>
        <FAQ items={PRICING_FAQ.items} />
      </section>

      <section
        className="hml-card hml-pricing-outro"
        aria-labelledby="pricing-outro-title"
      >
        <h2 id="pricing-outro-title">{PRICING_OUTRO.heading}</h2>
        <p>{PRICING_OUTRO.body}</p>
        <div className="hml-pricing-outro__cta">
          <Link
            className="hml-button hml-button--primary"
            to={PRICING_OUTRO.primaryCta.to}
          >
            {PRICING_OUTRO.primaryCta.label}
          </Link>
          <Link
            className="hml-button hml-button--secondary"
            to={PRICING_OUTRO.secondaryCta.to}
          >
            {PRICING_OUTRO.secondaryCta.label}
          </Link>
        </div>
      </section>
    </>
  );
}
