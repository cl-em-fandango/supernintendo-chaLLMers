import React from 'react';
import { NavLink } from 'react-router-dom';
import { NAV_ITEMS } from '../navigation';

export default function Header() {
  return (
    <header className="hml-header">
      <div className="hml-header__inner">
        <NavLink to="/" className="hml-brand" end>
          <span aria-hidden="true">✦ </span>Hannah Montana Linux
        </NavLink>
        <nav className="hml-nav" aria-label="Primary">
          {NAV_ITEMS.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === '/'}
              className={({ isActive }) =>
                isActive ? 'hml-nav__link active' : 'hml-nav__link'
              }
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
      </div>
    </header>
  );
}
