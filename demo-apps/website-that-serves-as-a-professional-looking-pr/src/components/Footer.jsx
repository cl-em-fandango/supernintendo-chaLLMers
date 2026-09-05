import React from 'react';
import { Link } from 'react-router-dom';
import { NAV_ITEMS } from '../navigation';

export default function Footer() {
  return (
    <footer className="hml-footer">
      <div className="hml-footer__inner">
        <nav aria-label="Footer">
          <ul className="hml-footer__links">
            {NAV_ITEMS.map((item) => (
              <li key={item.to}>
                <Link to={item.to}>{item.label}</Link>
              </li>
            ))}
          </ul>
        </nav>
        <p className="hml-footer__note">
          Hannah Montana Linux is a fictional parody product; this website is
          a mockup and not affiliated with anyone it jokingly name-drops.
        </p>
      </div>
    </footer>
  );
}
