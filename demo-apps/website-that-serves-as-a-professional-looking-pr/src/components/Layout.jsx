import React from 'react';
import { Outlet } from 'react-router-dom';
import Header from './Header';
import Footer from './Footer';

export default function Layout() {
  return (
    <>
      <a className="hml-skip-link" href="#main">
        Skip to main content
      </a>
      <Header />
      <main className="hml-container" id="main" tabIndex={-1}>
        <Outlet />
      </main>
      <Footer />
    </>
  );
}
