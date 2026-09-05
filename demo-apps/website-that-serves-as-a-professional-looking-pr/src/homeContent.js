// homeContent.js — copy and data for the Home view.
// Strings here are data only; rendering lives in the Home components.

export const HERO = Object.freeze({
  productName: 'Hannah Montana Linux',
  tagline: 'Best of Both Worlds. Best of Both Kernels.',
  pitch:
    'The enterprise-grade operating system for people who ship to production ' +
    'by day and headline the data center by night.',
  primaryCta: Object.freeze({ label: 'Download 100% Fake ISO', to: '/get' }),
  secondaryCta: Object.freeze({ label: 'Read the Docs', to: '/features' }),
});

export const HIGHLIGHTS = Object.freeze([
  Object.freeze({
    id: 'dual-persona-kernel',
    icon: '🎭',
    title: 'Dual-Persona Kernel',
    blurb:
      'One whoami, two identities. The hecklu.ko module loads at boot and ' +
      'switches between the miley and hannah runlevels before your sudo ' +
      'ticket has finished printing.',
  }),
  Object.freeze({
    id: 'sparkle-display-manager',
    icon: '✨',
    title: 'Sparkle Display Manager',
    blurb:
      'Hot pink and purple theming straight out of the box. Every login ' +
      'screen is stage-ready, and every screensaver is a light show.',
  }),
  Object.freeze({
    id: 'heck-a-security',
    icon: '🛡️',
    title: 'Heck-A-Security Module',
    blurb:
      "A firewall that asks \u201Cwho said that\u201D before any packet " +
      'gets an answer. Privilege escalation requires you to say \u201Cwoah\u201D ' +
      'out loud.',
  }),
  Object.freeze({
    id: 'hybrid-edition',
    icon: '🐧',
    title: 'Best of Both Worlds Hybrid',
    blurb:
      'A polished desktop OS — and a server OS, the other one, the one only ' +
      'you and a select few know about.',
  }),
]);

export const STATS = Object.freeze([
  Object.freeze({ id: 'install-base', value: '2', label: 'install base, worldwide' }),
  Object.freeze({ id: 'cves', value: '0', label: 'CVEs we acknowledge' }),
  Object.freeze({ id: 'fake-bits', value: '100%', label: 'fake ISO bits' }),
  Object.freeze({ id: 'personalities', value: '∞', label: 'personalities per kernel' }),
]);

export const TESTIMONIAL_TEASER = Object.freeze({
  heading: 'Don’t take our word for it',
  body:
    'Industry legends have weighed in on the release of Hannah Montana ' +
    'Linux — some with detailed technical assessments, one by simply asking ' +
    'how old the product is. Read the full wall of endorsements, straight ' +
    'from people who definitely reviewed the real thing.',
  ctaLabel: 'Read the testimonials',
  to: '/testimonials',
});
