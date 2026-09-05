// pricingContent.js — copy and data for the Pricing view.
// Strings here are data only; rendering lives in Pricing/PricingCard/FAQ.

export const PRICING_INTRO = Object.freeze({
  heading: 'Pricing For Every Persona',
  deck:
    'One kernel, two identities, three honest price points. Whether you run ' +
    'a single sparkle-rimmed laptop or a datacenter that hums along to the ' +
    '3 a.m. cron choir, there is a tier with your name on it — whichever ' +
    'name you are answering to this week. All prices are fictional, all ' +
    'tiers are equally imaginary, and our billing system is a sticky note ' +
    'on a monitor in a room that does not exist.',
});

export const PRICING_TIERS = Object.freeze([
  Object.freeze({
    id: 'miley-basic',
    name: 'Miley Basic',
    tagline: 'The wholesome one.',
    price: 'Free',
    priceNote: 'forever, like a childhood memory',
    accent: 'gold',
    featured: false,
    description:
      'Everything a responsible SAG (Sparklyly Assigned Guy) needs to boot, ' +
      'glitter, and dual-persona your way through a weekend build. Miley ' +
      'Basic is the honest, boots-and-kernel, no-frills side of the ' +
      'distributor: stable runlevels, a modest sparkle allowance, and a ' +
      'penguin who still wears the wig, because the wig is non-negotiable.',
    features: Object.freeze([
      Object.freeze({ label: 'Dual-Persona Kernel (miley runlevel)', included: true }),
      Object.freeze({ label: 'hecklu.ko loaded on boot', included: true }),
      Object.freeze({ label: 'Sparkle Display Manager, theme: “Sincere”', included: true }),
      Object.freeze({ label: 'Community support via knowing nods', included: true }),
      Object.freeze({ label: 'Heck-A-Security Module (who-said-that firewall)', included: false }),
      Object.freeze({ label: 'Cron choir at 3 a.m.', included: false }),
    ]),
    cta: Object.freeze({ label: 'Start Wholesome', to: '/get' }),
  }),
  Object.freeze({
    id: 'hannah-pro',
    name: 'Hannah Pro',
    tagline: 'The one on TV.',
    price: '$9.99/mo',
    priceNote: 'billed to a fictional card',
    accent: 'pink',
    featured: true,
    description:
      'The primetime tier. Hannah Pro unlocks the full stage-glamour ' +
      'compositor, the hannah runlevel, and priority patch-lights so your ' +
      'zero acknowledged CVEs stay that way. Teams pick this tier when they ' +
      'need production sparkle, auditable personas, and a firewall with ' +
      'attitude. Best of both worlds, one monthly confession.',
    features: Object.freeze([
      Object.freeze({ label: 'Everything in Miley Basic, but louder', included: true }),
      Object.freeze({ label: 'hannah runlevel + automatic persona failover', included: true }),
      Object.freeze({ label: 'Heck-A-Security Module (sudo requires saying “woah”)', included: true }),
      Object.freeze({ label: 'Full sparkle compositor, 60 fps of glitter', included: true }),
      Object.freeze({ label: 'Cron choir at 3 a.m. (harmonies included)', included: true }),
      Object.freeze({ label: 'The Other One privacy mode', included: false }),
    ]),
    cta: Object.freeze({ label: 'Go Primetime', to: '/get' }),
  }),
  Object.freeze({
    id: 'other-one-enterprise',
    name: 'The Other One Enterprise',
    tagline: 'If you have to ask, you can’t afford the unblurred screen name.',
    price: '▓▓▓▓▓',
    priceNote: 'redacted, as requested',
    accent: 'purple',
    featured: false,
    description:
      'You already know which tier this is. The Other One Enterprise ships ' +
      'the privacy mode that privacy modes aspire to: your sysadmin name is ' +
      'blurred in every log, your root shell performs its own intro music, ' +
      'and our account manager only contacts you through a single rose ' +
      'left on your rack unit. Includes everything above, plus discretion ' +
      'measured in whole numbers we are not allowed to print.',
    features: Object.freeze([
      Object.freeze({ label: 'Everything in Hannah Pro, uncredited', included: true }),
      Object.freeze({ label: 'The Other One privacy mode (blurred screen name)', included: true }),
      Object.freeze({ label: 'Dedicated stage crew (your TAM, in sequins)', included: true }),
      Object.freeze({ label: '24/7 hotline that answers “who said that” first', included: true }),
      Object.freeze({ label: 'On-site wig compliance audits', included: true }),
      Object.freeze({ label: 'Public acknowledgment of this tier’s existence', included: false }),
    ]),
    cta: Object.freeze({ label: 'Talk To Sales (You Won’t Get Far)', to: '/get' }),
  }),
]);

export const PRICING_FAQ = Object.freeze({
  heading: 'Frequently Asked Questions',
  items: Object.freeze([
    Object.freeze({
      id: 'really-free',
      question: 'Is Miley Basic actually free?',
      answer:
        'It is actually fictionally free, which is strictly better: nothing ' +
        'real changes hands. You will not be billed, our invoice engine is a ' +
        'music box, and the only recurring charge is the cron choir, which ' +
        'bills you in mild sleep deprivation and show tunes.',
    }),
    Object.freeze({
      id: 'switch-personas',
      question: 'Can I switch tiers, or even switch personas, mid-cycle?',
      answer:
        'Both. Upgrading from Miley to Hannah is a single reboot and one ' +
        'confident hair flip. Downgrading is technically supported, ' +
        'emotionally difficult, and leaves a single pink theme file behind ' +
        'like an autograph on a bathroom wall.',
    }),
    Object.freeze({
      id: 'enterprise-price',
      question: 'Why is the Enterprise price redacted?',
      answer:
        'Because the tier’s terms state that if you have to ask, you cannot ' +
        'afford the unblurred screen name, and our legal team (a mirror and ' +
        'a whisper) takes that literally. Serious inquiries are answered ' +
        'with a single rose and an NDA written in glitter glue.',
    }),
    Object.freeze({
      id: 'refund-policy',
      question: 'What is the refund policy?',
      answer:
        'Every plan is backed by our 100% imaginary money-back guarantee: ' +
        'if Hannah Montana Linux ever fails to be a mockup, reply to no ' +
        'invoice and we will refund nothing to you in full. This has ' +
        'happened zero times out of zero sales.',
    }),
  ]),
});

export const PRICING_OUTRO = Object.freeze({
  heading: 'Still Comparing?',
  body:
    'Every tier above installs the same gloriously fake operating system ' +
    'and answers to the same penguin in the same blonde wig. Pick the one ' +
    'whose tagline fits your mood, click through, and enjoy a download ' +
    'button that promises exactly what it delivers: nothing, beautifully.',
  primaryCta: Object.freeze({ label: 'Download The Mockup', to: '/get' }),
  secondaryCta: Object.freeze({ label: 'Read The Features', to: '/features' }),
});
