import React from 'react';
import { Route, Routes } from 'react-router-dom';
import Layout from './components/Layout';
import Home from './pages/Home';
import Features from './pages/Features';
import Testimonials from './pages/Testimonials';
import Pricing from './pages/Pricing';
import GetHML from './pages/GetHML';

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route path="/" element={<Home />} />
        <Route path="/features" element={<Features />} />
        <Route path="/testimonials" element={<Testimonials />} />
        <Route path="/pricing" element={<Pricing />} />
        <Route path="/get" element={<GetHML />} />
        <Route path="*" element={<Home />} />
      </Route>
    </Routes>
  );
}
