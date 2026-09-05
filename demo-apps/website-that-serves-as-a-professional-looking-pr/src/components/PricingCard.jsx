import React from 'react';
import { Link } from 'react-router-dom';

// PricingCard.jsx — one pricing tier: name, tagline, price, description,
// a checkmark feature list, and a CTA link. `tier.accent` selects a
// gold/pink/purple edge; `tier.featured` adds the "Most Sparkle" ribbon.
export default function PricingCard({ tier }) {
  return (
    <article
      className={`hml-card hml-tier hml-tier--${tier.accent}${
        tier.featured ? ' hml-tier--featured' : ''
      }`}
      aria-labelledby={`tier-${tier.id}-name`}
    >
      {tier.featured && (
        <p className="hml-tier__ribbon" aria-hidden="true">
          ✦ Most Sparkle ✦
        </p>
      )}
      <h3 id={`tier-${tier.id}-name`} className="hml-tier__name">
        {tier.name}
      </h3>
      <p className="hml-tier__tagline">{tier.tagline}</p>
      <p className="hml-tier__price">
        {tier.price}
        <span className="hml-tier__price-note">{tier.priceNote}</span>
      </p>
      <p className="hml-tier__description">{tier.description}</p>
      <ul className="hml-tier__features">
        {tier.features.map((feature) => (
          <li
            key={feature.label}
            className={
              feature.included
                ? 'hml-tier__feature hml-tier__feature--included'
                : 'hml-tier__feature hml-tier__feature--excluded'
            }
          >
            <span className="hml-tier__check" aria-hidden="true">
              {feature.included ? '✓' : '✕'}
            </span>
            {feature.label}
          </li>
        ))}
      </ul>
      <Link
        className={`hml-button hml-button--${
          tier.featured ? 'primary' : 'secondary'
        }`}
        to={tier.cta.to}
      >
        {tier.cta.label}
      </Link>
    </article>
  );
}
