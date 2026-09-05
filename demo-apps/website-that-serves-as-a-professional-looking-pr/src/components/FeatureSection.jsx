import React from 'react';

// FeatureSection.jsx — renders one Features-view subsection: heading,
// deck line, prose paragraphs, a bullet list, and a terminal-styled
// code snippet on a dark surface. All strings arrive via props from
// featuresContent.js; nothing here is copy.

function TerminalWindow({ terminal }) {
  return (
    <div className="hml-terminal">
      <div className="hml-terminal__bar" aria-hidden="true">
        <span className="hml-terminal__dots">
          <span className="hml-terminal__dot" />
          <span className="hml-terminal__dot" />
          <span className="hml-terminal__dot" />
        </span>
        <span className="hml-terminal__title">{terminal.title}</span>
      </div>
      <pre className="hml-terminal__body">
        <code>{terminal.lines.join('\n')}</code>
      </pre>
    </div>
  );
}

export default function FeatureSection({ feature }) {
  return (
    <section
      id={feature.id}
      className="hml-card hml-feature"
      aria-labelledby={`${feature.id}-title`}
    >
      <h2 id={`${feature.id}-title`} className="hml-feature__title">
        <span className="hml-feature__icon" aria-hidden="true">
          {feature.icon}
        </span>
        {feature.title}
      </h2>
      <p className="hml-feature__deck">{feature.deck}</p>
      {feature.paragraphs.map((text, index) => (
        <p key={`${feature.id}-p-${index}`} className="hml-feature__paragraph">
          {text}
        </p>
      ))}
      <ul className="hml-feature__bullets">
        {feature.bullets.map((item, index) => (
          <li key={`${feature.id}-li-${index}`}>{item}</li>
        ))}
      </ul>
      <TerminalWindow terminal={feature.terminal} />
    </section>
  );
}
