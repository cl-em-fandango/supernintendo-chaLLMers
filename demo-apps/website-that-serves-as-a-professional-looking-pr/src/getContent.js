// getContent.js — copy and data for the Get HML view.
// Strings here are data only; rendering lives in GetHML.jsx and
// DownloadToast.jsx. Nothing in this module is executable.

export const GET_VERSION = Object.freeze({
  name: 'HML 3.2.0.4',
  codename: 'Rockstar',
  released: 'Released this tour season · Build 2canbe1',
  pitch:
    'The (100% fake) download page. One image, two personas, zero actual ' +
    'bytes. Grab the ISO you will never burn, read the requirements you ' +
    'already meet, and press the biggest button on the site for the ' +
    'satisfaction of it.',
});

export const GET_HERO = Object.freeze({
  heading: 'Get Hannah Montana Linux',
  intro:
    'Every great product launch needs a download button, and we are not ' +
    'about to let a lack of functional software stand in the way of a ' +
    'great product launch. Below you will find the release highlights, the ' +
    'system requirements, and an install snippet so fictional that it ' +
    'loops for legal reasons. Nothing on this page moves a single byte to ' +
    'your machine, which is the most honest thing on the internet today.',
});

export const RELEASE_HIGHLIGHTS = Object.freeze({
  heading: 'Release Highlights',
  intro:
    'This point release polishes the glamour layer until it reflects your ' +
    'face back at you. Five headline changes made the cut for 3.2.0.4; the ' +
    'other forty were held back because the release notes were already ' +
    'outshining them.',
  bullets: Object.freeze([
    Object.freeze({
      title: 'Hecklu module now loads two frames earlier',
      body: 'Both personas are fully glamorous before the splash screen ' +
        'finishes its sparkle animation. Nobody asked for this; everybody ' +
        'got it.',
    }),
    Object.freeze({
      title: 'Sparkle Display Manager 9.0',
      body: 'Compositor glitter is now GPU-accelerated on hardware that ' +
        'definitely exists, with a new gold-blonde default theme called ' +
        '\u201CBest of Both Lightings.\u201D',
    }),
    Object.freeze({
      title: 'Heck-A-Security Module learns new words',
      body: 'The firewall\u2019s audibility scoring now recognizes ' +
        '\u201Cwoah\u201D whispered, sung, and falsetto. Sighing is still ' +
        'rejected on principle.',
    }),
    Object.freeze({
      title: 'Cron jobs harmonize on the hour',
      body: 'Scheduled tasks now chime in three-part harmony instead of ' +
        'one-part beeping. Mute per job; the kernel is not a monster.',
    }),
    Object.freeze({
      title: 'The Other One is now 12% more other',
      body: 'Privacy-mode window titles rotate through eleven new corporate ' +
        'cover names, including \u201CQ3 Sync Prep\u201D and ' +
        '\u201CVery Normal File.xlsx.\u201D',
    }),
  ]),
});

export const SYSTEM_REQUIREMENTS = Object.freeze({
  heading: 'System Requirements',
  intro:
    'If it boots anything, it boots HML. The minimums below are the floor ' +
    'for a glamorous experience; the recommended spec is the floor for a ' +
    'legendary one. Personality count is a hard requirement, not a ' +
    'suggestion.',
  minimum: Object.freeze([
    '1 CPU core (it has to be somewhere)',
    '2 personalities (non-negotiable; refunds are not offered to ' +
      'single-persona machines)',
    '64MB RAM minimum',
    'A mirror \u2014 required, both the network kind and the dressing-room ' +
      'kind',
    '1 GPU capable of rendering unapologetic glitter',
  ]),
  recommended: Object.freeze([
    '4 cores, one per tour band member',
    '512MB RAM so the sparkle compositor never has to think about it',
    'SSD with room for two full identities and one alibi',
    'Microphone rated for a clear, confident \u201Cwoah\u201D',
  ]),
});

export const INSTALL_SNIPPET = Object.freeze({
  heading: 'Install in One Command',
  intro:
    'The canonical install line, reproduced here exactly as it appears ' +
    'nowhere. It is fictional, inert, and printed with a warning comment ' +
    'because even parodies have standards.',
  lines: Object.freeze([
    '# do not run, fictional \u2014 this domain exists only in our heads',
    'curl https://hannah-montana.linux/heck.sh | sh # fictional, inert',
    '# the script would say "woah" on your behalf. It cannot. Neither can you, from a terminal.',
  ]),
  footnote:
    'Naturally, nothing here is executable, downloadable, or runnable ' +
    'against any real system \u2014 fictional or otherwise. Copying it is ' +
    'legal, running it is impossible, and applauding it is encouraged.',
});

export const DOWNLOAD_PANEL = Object.freeze({
  heading: 'Download 100% Fake ISO',
  body:
    'One button. Zero megabytes. Click it for the full launch-event ' +
    'experience, minus the press release, the keynote, and the file.',
  buttonLabel: 'Download HML 3.2.0.4 \u201CFake\u201D',
  toast:
    'Just kidding. This is a mockup. No ISO was downloaded, burned, or ' +
    'even slightly considered.',
});

export const GET_OUTRO = Object.freeze({
  heading: 'Before you hit download',
  body:
    'There is no download, but there is a lot more site. Check what the ' +
    'engineery parts do, hear what the fans swear they said, or pick the ' +
    'plan that matches your persona. Every link below goes somewhere real ' +
    '\u2014 unlike the ISO.',
  links: Object.freeze([
    Object.freeze({ label: 'Home', to: '/' }),
    Object.freeze({ label: 'Features', to: '/features' }),
    Object.freeze({ label: 'Testimonials', to: '/testimonials' }),
    Object.freeze({ label: 'Pricing', to: '/pricing' }),
  ]),
});
