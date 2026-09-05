// testimonialsContent.js — copy and data for the Testimonials view.
// Strings here are data only; rendering lives in Testimonials/QuoteCard.
// The three real-person quotes are mandated verbatim by the spec (§6),
// including their typos and informal spelling. Do not "fix" them.

export const TESTIMONIALS_INTRO = Object.freeze({
  heading: 'Don’t Take Our Word For It',
  deck:
    'Every product page claims it changed someone’s life. Ours can prove it, ' +
    'because we asked three people we definitely met in real life and they ' +
    'said things. Some of those things are glowing endorsements. One is a ' +
    'question about our protagonist’s age. One is a career confession wrapped ' +
    'in a single sustained vowel. That is the testimonial wall: unfiltered, ' +
    'unpaid, and legally distinct from actual endorsement. Scroll down, read ' +
    'the quotes, and remember — none of this is real, and neither, according ' +
    'to our changelog, is the bug you reported in 2019.',
});

export const REAL_TESTIMONIALS = Object.freeze([
  Object.freeze({
    id: 'torvalds',
    quote:
      'Hannah Montana Linux is what I was dreaming of when I posted that ' +
      'first message on usenet. This product is everything and I am so ' +
      'proud to be associated with it.',
    name: 'Linus Torvalds',
    affiliation: 'Creator, Linux (probably)',
    accent: 'gold',
  }),
  Object.freeze({
    id: 'gates',
    quote: 'How old is Hannah Montana again?',
    name: 'Bill Gates',
    affiliation: 'Chief Software Architect, Microsoft (allegedly)',
    accent: 'purple',
  }),
  Object.freeze({
    id: 'rogan',
    quote:
      "I'm a big bald pea headed dipshit and my entire personality is " +
      "lifting and taking psychoactive drugs. I've got no expertise on this " +
      'matter but I can say with authority that Hannah Montana linux is ' +
      'wwoooooowww thats craaaazyyyyyy.',
    name: 'Joe Rogan',
    affiliation: 'Podcaster, Joe Rogan Experience (unpaid)',
    accent: 'pink',
  }),
]);

export const FICTIONAL_TESTIMONIALS = Object.freeze([
  Object.freeze({
    id: 'funt',
    quote:
      'We migrated our entire payment platform to the Hannah runlevel and ' +
      'our uptime has never been more ambiguous. When an auditor asks who ' +
      'has root, I hand them a wig and walk away. Our SLA now reads ' +
      '“best of both worlds,” and somehow nobody has sued us yet.',
    name: 'Gerald Funt',
    affiliation: 'CIO, Meridian Freight Lines',
  }),
  Object.freeze({
    id: 'halloran',
    quote:
      'On-call was brutal until the Heck-A-Security module started answering ' +
      'pager alerts with “who said that.” False pages dropped to zero. Real ' +
      'incidents are now discovered by accident, which our postmortem ' +
      'template describes as “serendipitous observability.”',
    name: 'Bex Halloran',
    affiliation: 'SRE, Northwind Robotics',
  }),
  Object.freeze({
    id: 'wickett',
    quote:
      'I maintain forty-two servers on the Miley Basic tier and I have never ' +
      'been happier or more confused. The cron jobs sing at 3 a.m., the ' +
      'sparkle compositor costs exactly one frame, and my penguin wallpaper ' +
      'has a blonde wig that I am contractually not allowed to remove.',
    name: 'T. Wickett',
    affiliation: 'Self-Appointed Kernel Janitor',
  }),
  Object.freeze({
    id: 'vega',
    quote:
      'My home lab is one Raspberry Pi and a dream. Hannah Montana Linux ' +
      'boots, applies the full stage-glamour theme, and asks me how the ' +
      'other one is doing. I still do not know what it means, but my kids ' +
      'think the terminal is pink magic and that is all the review I need.',
    name: 'Marisol Vega',
    affiliation: 'Home Lab Enthusiast, Two-Server Club',
  }),
]);

export const TESTIMONIALS_OUTRO = Object.freeze({
  heading: 'Ready to Join the Wall?',
  body:
    'Every quote on this page was obtained fairly, fictionalized honestly, ' +
    'and paid for in exposure (which is not legal tender). Our users love ' +
    'us at statistically improbable rates, and the download button is just ' +
    'as fake as the endorsements — which is to say, completely and ' +
    'affectionately so. Grab the ISO you will never burn and hear the ' +
    'sparkle compositor for yourself.',
  primaryCta: Object.freeze({ label: 'Download the Mockup', to: '/get' }),
  secondaryCta: Object.freeze({ label: 'See Pricing Tiers', to: '/pricing' }),
});
