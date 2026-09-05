import React from 'react';
import { Link } from 'react-router-dom';
import StageMascot from './StageMascot';

// Hero.jsx — the Home view's headline block: product name, tagline,
// one-line pitch, and the two primary calls to action.
export default function Hero({ hero }) {
  return (
    <section className="hml-hero" aria-labelledby="hml-hero-title">
      <p className="hml-hero__sparkle" aria-hidden="true">
        ✦ ✧ ✦
      </p>
      <h1 id="hml-hero-title">{hero.productName}</h1>
      <p className="hml-hero__tagline">{hero.tagline}</p>
      <p className="hml-hero__pitch">{hero.pitch}</p>
      <p className="hml-hero__prompt" aria-hidden="true">
        <code>hml@both-worlds:~$ whoami</code>
      </p>
      <div className="hml-hero__cta">
        <Link className="hml-button hml-button--primary" to={hero.primaryCta.to}>
          {hero.primaryCta.label}
        </Link>
        <Link className="hml-button hml-button--secondary" to={hero.secondaryCta.to}>
          {hero.secondaryCta.label}
        </Link>
      </div>
      <StageMascot />
    </section>
  );
}
