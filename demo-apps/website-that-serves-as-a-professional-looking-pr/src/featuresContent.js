// featuresContent.js — copy and data for the Features view.
// Strings here are data only; rendering lives in FeatureSection.

export const FEATURES_INTRO = Object.freeze({
  heading: 'Features',
  deck:
    'Every feature ships in a single image, because a rockstar never makes ' +
    'the audience pick a favorite track. Below is the official engineering ' +
    'tour: real-sounding subsystem names, completely fictional guarantees, and ' +
    'terminal transcripts that never happened on any machine. Read it like ' +
    'release notes from 2007, but with better lighting.',
});

export const FEATURES = Object.freeze([
  Object.freeze({
    id: 'dual-persona-kernel',
    icon: '🎭',
    title: 'Dual-Persona Kernel',
    deck: 'One machine. Two identities. Zero identity crises.',
    paragraphs: [
      'The moment initramfs loads hecklu.ko — the Hecklu module — before the ' +
        'root filesystem is even writable, every process on the box inherits ' +
        'not one identity but two. Run whoami and the kernel answers ' +
        'honestly.',
      'Persona switching is first-class. The miley ' +
        'runlevel is wholesome, multi-user, and fine for daytime workloads; ' +
        'the hannah runlevel unlocks the full stage scheduler, extra sparkle ' +
        'interrupts, and a MOTD that knows your secrets.',
    ],
    bullets: [
      'whoami returns two identities on every TTY, cron job, and ssh session.',
      'Runlevels miley and hannah are both default-safe; telinit hannah switches without a reboot.',
      'hecklu.ko is signed, taint-free, and refuses to be unloaded before intermission.',
    ],
    terminal: Object.freeze({
      title: 'hml@stage:~',
      lines: Object.freeze([
        '$ whoami',
        'miley',
        'hannah',
        '$ systemctl status hecklu.ko',
        '● hecklu.module — Loaded, glamorous, and not going anywhere',
        '   Active: active (running) since tour start',
      ]),
    }),
  }),
  Object.freeze({
    id: 'best-of-both-worlds-hybrid',
    icon: '🌍',
    title: 'Best of Both Worlds Hybrid',
    deck: 'A desktop OS, a server OS, and one nobody else can see.',
    paragraphs: [
      'Hannah Montana Linux is a polished desktop environment and a hardened ' +
        'server platform in the same install. The desktop half is the one ' +
        'everyone knows about. The server half is the other one — the one ' +
        'that only you and a select few know exists.',
      'The other one shares no mount points, no host keys, and no alibi with ' +
        'the visible system. Your monitoring stack cannot graph it, your ' +
        'auditor cannot invoice it, and your manager cannot standup it.',
    ],
    bullets: [
      'One ISO boots into workstation, server, or the other one.',
      'The control panel looks like a vanity mirror but manages nginx.',
      'Discovery of the other one requires a passphrase and a vibe check.',
    ],
    terminal: Object.freeze({
      title: 'hml@backstage:~',
      lines: Object.freeze([
        '$ hml-mode --reveal',
        'You probably shouldn\u2019t be looking at this.',
        'nginx is listening on a port that officially does not exist.',
      ]),
    }),
  }),
  Object.freeze({
    id: 'sparkle-display-manager',
    icon: '✨',
    title: 'Sparkle Display Manager',
    deck: 'Pink and purple out of the box. Glitter at the GPU level.',
    paragraphs: [
      'Most display managers ship a theme. Ours ships a wardrobe. The Sparkle ' +
        'Display Manager renders hot pink and deep purple compositing ' +
        'straight out of the box — no dotfiles, no ricer forums, no ' +
        'apologies — with gold-blonde accent lighting baked into the ' +
        'compositor itself.',
      'The package manager understands the assignment: dual-instance ' +
        'resolution is a supported flag, and every transaction ends on a ' +
        'thirty-second chime legal cleared as \u201Cnot a song\u201D.',
    ],
    bullets: [
      'apt install --two-can-be-hannah-montana resolves conflicts by letting both packages win.',
      'Lock screen scatters GPU-accelerated glitter on every failed login attempt.',
      'Gold, hot pink, and purple themes are the only supported palettes; support declines to comment on beige.',
    ],
    terminal: Object.freeze({
      title: 'root@glamour:~',
      lines: Object.freeze([
        '# apt install --two-can-be-hannah-montana sparkle-theme',
        'Reading package lists... Done',
        'Building dependency tree... Done',
        'The following package was upgraded: everybody',
      ]),
    }),
  }),
  Object.freeze({
    id: 'heck-a-security-module',
    icon: '🛡️',
    title: 'Heck-A-Security Module',
    deck: 'A firewall with opinions and sudo with a microphone.',
    paragraphs: [
      'The Heck-A-Security Module inspects every inbound packet and asks the ' +
        'only question that matters: who said that. Traffic without a strong ' +
        'alibi is dropped, logged, and in extreme cases told that is hecka ' +
        'bold. Outbound traffic is not blocked, just disappointed.',
      'Privilege escalation is voice-gated: sudo requires saying ' +
        '\u201Cwoah\u201D out loud into the system microphone, and the ' +
        'module rejects sighing, mumbling, and sarcasm.',
    ],
    bullets: [
      'Stateful firewall with per-packet audibility scoring.',
      'sudo prompts for a spoken \u201Cwoah\u201D; three strikes and it gossips to /var/log.',
      'The Nobody\u2019s Perfect IDS flags every anomaly, then admits it is imperfect too.',
    ],
    terminal: Object.freeze({
      title: 'hml@vault:~',
      lines: Object.freeze([
        '$ sudo mv /etc/motd /etc/motd.bak',
        '[sudo] say "woah" to continue: woah',
        'woah accepted. You now have heck-a-permissions.',
      ]),
    }),
  }),
  Object.freeze({
    id: 'the-other-one-privacy-mode',
    icon: '🕶️',
    title: 'The Other One Privacy Mode',
    deck: 'Instantly looks like someone else\u2019s laptop.',
    paragraphs: [
      'One keystroke dims the sparkle layer, swaps the wallpaper to a tasteful ' +
        'corporate gray, and renames the hostname to devops-box. Every window ' +
        'title becomes \u201CQuarterly Report\u201D, including the ones ' +
        'playing concert footage.',
    ],
    bullets: [
      'Super+Escape toggles incognito glamour in under one frame.',
      'hml other-one --status reports \u201Cyou probably shouldn\u2019t be looking at this\u201D.',
      'Exiting the mode restores every sparkle exactly where it was left.',
    ],
    terminal: Object.freeze({
      title: 'hml@devops-box:~',
      lines: Object.freeze([
        '$ hml other-one --status',
        'you probably shouldn\u2019t be looking at this',
      ]),
    }),
  }),
]);

export const FEATURES_OUTRO = Object.freeze({
  heading: 'Ready for the encore?',
  body:
    'The full tour schedule lives on the download page, and the fan club ' +
    'has opinions. Take the stage or hear what the front row is saying.',
  primaryCta: Object.freeze({ label: 'Get HML', to: '/get' }),
  secondaryCta: Object.freeze({ label: 'Read Testimonials', to: '/testimonials' }),
});
