import React from 'react';

// FAQ.jsx — a collapsible Q&A list. Each item renders as a native
// <details>/<summary> pair: keyboard accessible, no JavaScript state.
export default function FAQ({ items }) {
  return (
    <div className="hml-faq__list">
      {items.map((item) => (
        <details key={item.id} className="hml-faq__item">
          <summary className="hml-faq__question">{item.question}</summary>
          <p className="hml-faq__answer">{item.answer}</p>
        </details>
      ))}
    </div>
  );
}
