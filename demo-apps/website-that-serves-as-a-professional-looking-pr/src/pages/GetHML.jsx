import React, { useState } from 'react';
import { Link } from 'react-router-dom';
import {
  GET_VERSION,
  GET_HERO,
  RELEASE_HIGHLIGHTS,
  SYSTEM_REQUIREMENTS,
  INSTALL_SNIPPET,
  DOWNLOAD_PANEL,
  GET_OUTRO,
} from '../getContent';
import DownloadToast from '../components/DownloadToast';
import '../get.css';

// GetHML.jsx — composition of the Get HML route: version header, release
// highlights, system requirements, an inert install snippet, the fake
// download button with its in-page toast, and a repeated site-links area.
// All copy lives in getContent.js; the button only toggles local state and
// never touches the network or the filesystem (FR-6).
export default function GetHML() {
  const [toastVisible, setToastVisible] = useState(false);

  return (
    <>
      <section className="hml-section" aria-labelledby="get-title">
        <p className="hml-get__versionline">
          <span className="hml-get__version">{GET_VERSION.name}</span>
          <span className="hml-get__codename">
            &ldquo;{GET_VERSION.codename}&rdquo;
          </span>
        </p>
        <h1 id="get-title">{GET_HERO.heading}</h1>
        <p className="hml-get__deck">{GET_VERSION.pitch}</p>
        <p className="hml-get__released">{GET_VERSION.released}</p>
        <p className="hml-get__intro">{GET_HERO.intro}</p>
      </section>

      <section className="hml-section" aria-labelledby="highlights-title">
        <h2 id="highlights-title" className="hml-get__title">
          <span className="hml-get__sparkle" aria-hidden="true">
            ✦
          </span>
          {RELEASE_HIGHLIGHTS.heading}
        </h2>
        <p className="hml-get__intro">{RELEASE_HIGHLIGHTS.intro}</p>
        <div className="hml-grid hml-get__highlights">
          {RELEASE_HIGHLIGHTS.bullets.map((bullet) => (
            <article
              key={bullet.title}
              className="hml-card hml-get__highlight"
            >
              <h3 className="hml-get__highlight-title">{bullet.title}</h3>
              <p className="hml-get__highlight-body">{bullet.body}</p>
            </article>
          ))}
        </div>
      </section>

      <section className="hml-section" aria-labelledby="requirements-title">
        <h2 id="requirements-title" className="hml-get__title">
          <span className="hml-get__sparkle" aria-hidden="true">
            ✦
          </span>
          {SYSTEM_REQUIREMENTS.heading}
        </h2>
        <p className="hml-get__intro">{SYSTEM_REQUIREMENTS.intro}</p>
        <div className="hml-grid hml-get__requirements">
          <div className="hml-card hml-get__reqcard">
            <h3 className="hml-get__reqcard-title">Minimum</h3>
            <ul className="hml-get__reqlist">
              {SYSTEM_REQUIREMENTS.minimum.map((item) => (
                <li key={item} className="hml-get__req">
                  <span className="hml-get__req-check" aria-hidden="true">
                    ✓
                  </span>
                  {item}
                </li>
              ))}
            </ul>
          </div>
          <div className="hml-card hml-get__reqcard">
            <h3 className="hml-get__reqcard-title">Recommended</h3>
            <ul className="hml-get__reqlist">
              {SYSTEM_REQUIREMENTS.recommended.map((item) => (
                <li key={item} className="hml-get__req">
                  <span className="hml-get__req-check" aria-hidden="true">
                    ✦
                  </span>
                  {item}
                </li>
              ))}
            </ul>
          </div>
        </div>
      </section>

      <section className="hml-section" aria-labelledby="install-title">
        <h2 id="install-title" className="hml-get__title">
          <span className="hml-get__sparkle" aria-hidden="true">
            ✦
          </span>
          {INSTALL_SNIPPET.heading}
        </h2>
        <p className="hml-get__intro">{INSTALL_SNIPPET.intro}</p>
        <div className="hml-get__terminal" aria-label="Fictional install snippet">
          <p className="hml-get__terminal-bar" aria-hidden="true">
            <span className="hml-get__terminal-dot" />
            <span className="hml-get__terminal-dot" />
            <span className="hml-get__terminal-dot" />
            hml-installer — fictional shell
          </p>
          <pre>
            <code>
              {INSTALL_SNIPPET.lines.map((line) => (
                <React.Fragment key={line}>
                  {line}
                  {'\n'}
                </React.Fragment>
              ))}
            </code>
          </pre>
        </div>
        <p className="hml-get__footnote">{INSTALL_SNIPPET.footnote}</p>
      </section>

      <section
        className="hml-card hml-get__download"
        aria-labelledby="download-title"
      >
        <h2 id="download-title">{DOWNLOAD_PANEL.heading}</h2>
        <p>{DOWNLOAD_PANEL.body}</p>
        <button
          type="button"
          className="hml-button hml-button--primary hml-get__download-button"
          onClick={() => setToastVisible(true)}
        >
          {DOWNLOAD_PANEL.buttonLabel}
        </button>
        {toastVisible && (
          <DownloadToast
            message={DOWNLOAD_PANEL.toast}
            onDismiss={() => setToastVisible(false)}
          />
        )}
      </section>

      <section className="hml-section" aria-labelledby="get-outro-title">
        <h2 id="get-outro-title">{GET_OUTRO.heading}</h2>
        <p className="hml-get__intro">{GET_OUTRO.body}</p>
        <ul className="hml-get__links">
          {GET_OUTRO.links.map((item) => (
            <li key={item.to}>
              <Link to={item.to}>{item.label}</Link>
            </li>
          ))}
        </ul>
      </section>
    </>
  );
}
