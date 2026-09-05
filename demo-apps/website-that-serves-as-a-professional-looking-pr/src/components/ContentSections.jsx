import React from 'react';

// ContentSections.jsx — renders the baked-in content.json sections as
// semantic prose. Body text is data: paragraph lines render as <p>, and
// lines starting with the bullet character render inside a <ul>.

const BULLET_PREFIX = '\u2022';

function toBlocks(body) {
  const lines = body
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean);

  const blocks = [];
  let bullets = [];

  const flushBullets = () => {
    if (bullets.length > 0) {
      blocks.push(
        <ul key={`ul-${blocks.length}`} className="hml-prose__list">
          {bullets.map((item, index) => (
            <li key={`ul-${blocks.length}-${index}`}>{item}</li>
          ))}
        </ul>
      );
      bullets = [];
    }
  };

  lines.forEach((line) => {
    if (line.startsWith(BULLET_PREFIX)) {
      bullets.push(line.slice(BULLET_PREFIX.length).trim());
    } else {
      flushBullets();
      blocks.push(
        <p key={`p-${blocks.length}`} className="hml-prose__paragraph">
          {line}
        </p>
      );
    }
  });
  flushBullets();
  return blocks;
}

export default function ContentSections({ sections }) {
  return (
    <div className="hml-prose">
      {sections.map((section, index) => (
        <section key={`section-${index}`} className="hml-prose__section">
          <h2>{section.heading}</h2>
          {toBlocks(section.body)}
        </section>
      ))}
    </div>
  );
}
