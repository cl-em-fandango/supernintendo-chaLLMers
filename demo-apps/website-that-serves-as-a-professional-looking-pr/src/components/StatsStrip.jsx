import React from 'react';

// StatsStrip.jsx — the "trusted by" band of entirely fabricated numbers.
export default function StatsStrip({ stats }) {
  return (
    <section className="hml-stats" aria-label="Hannah Montana Linux by the numbers">
      <ul className="hml-stats__list">
        {stats.map((stat) => (
          <li key={stat.id} className="hml-stats__item">
            <span className="hml-stats__value">{stat.value}</span>
            <span className="hml-stats__label">{stat.label}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}
