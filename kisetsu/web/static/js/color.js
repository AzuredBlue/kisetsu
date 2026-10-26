// Accent and surface colours. Everything is an "r g b" triple string, so CSS can add alpha with rgb(var(--ac) / .5).

export const DEFAULT_ACCENT = '#2dd4bf';
export const ACCENT_PRESETS = [
  ['Teal', '#2dd4bf'], ['Sky', '#38bdf8'], ['Indigo', '#818cf8'], ['Violet', '#a78bfa'], ['Pink', '#f472b6'],
  ['Rose', '#fb7185'], ['Orange', '#fb923c'], ['Amber', '#fbbf24'], ['Lime', '#a3e635'], ['Emerald', '#34d399'],
];
export const ACCENT_TINTS = { off: 0, subtle: 0.22, full: 0.5 };
const STOCK_ACCENT = {
  dark: { ac: '45 212 191', soft: '94 234 212', strong: '20 184 166', ink: '4 32 30', ac2: '56 189 248' },
  light: { ac: '13 148 136', soft: '10 92 86', strong: '15 118 110', ink: '255 255 255', ac2: '2 132 199' },
};
// How strongly the page surfaces take the hue (0 keeps them grey).
const THEME_TINT = 0.6;

export function hslToRgb(h, s, l) {
  const c = (1 - Math.abs(2 * l - 1)) * s;
  const x = c * (1 - Math.abs(((h / 60) % 2) - 1));
  const m = l - c / 2;
  const [r, g, b] = h < 60 ? [c, x, 0] : h < 120 ? [x, c, 0] : h < 180 ? [0, c, x] : h < 240 ? [0, x, c] : h < 300 ? [x, 0, c] : [c, 0, x];
  return [r + m, g + m, b + m].map(v => Math.round(v * 255));
}

export function relativeLuminance([r, g, b]) {
  const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
  return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
}

export function contrast(a, b) {
  const [hi, lo] = [relativeLuminance(a), relativeLuminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

// Light-mode accents are worked out in OKLCH: darkening a colour in HSL drags its hue (a violet
// at 40% lightness comes out blue), while in OKLCH a colour keeps its hue as it darkens.
function srgbToLinear(v) {
  v /= 255;
  return v <= 0.04045 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
}

export function rgbToOklch([r, g, b]) {
  const [lr, lg, lb] = [r, g, b].map(srgbToLinear);
  const l = Math.cbrt(0.4122214708 * lr + 0.5363325363 * lg + 0.0514459929 * lb);
  const m = Math.cbrt(0.2119034982 * lr + 0.6806995451 * lg + 0.1073969566 * lb);
  const s = Math.cbrt(0.0883024619 * lr + 0.2817188376 * lg + 0.6299787005 * lb);
  const L = 0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s;
  const a = 1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s;
  const bb = 0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s;
  return [L, Math.hypot(a, bb), (Math.atan2(bb, a) * 180 / Math.PI + 360) % 360];
}

function oklchToLinear(L, C, h) {
  const a = C * Math.cos(h * Math.PI / 180), b = C * Math.sin(h * Math.PI / 180);
  const l = Math.pow(L + 0.3963377774 * a + 0.2158037573 * b, 3);
  const m = Math.pow(L - 0.1055613458 * a - 0.0638541728 * b, 3);
  const s = Math.pow(L - 0.0894841775 * a - 1.2914855480 * b, 3);
  return [
    4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
    -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
    -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s,
  ];
}

function oklchToRgb(L, C, h) {
  return oklchToLinear(L, C, h).map(v => {
    const c = v <= 0.0031308 ? v * 12.92 : 1.055 * Math.pow(v, 1 / 2.4) - 0.055;
    return Math.round(Math.min(1, Math.max(0, c)) * 255);
  });
}

// The largest chroma up to C that stays inside sRGB: pulling a colour towards grey keeps its hue,
// clipping a channel would shift it.
function fitChroma(L, C, h) {
  const inGamut = (c) => oklchToLinear(L, c, h).every(v => v >= -0.0005 && v <= 1.0005);
  if (inGamut(C)) return C;
  let lo = 0, hi = C;
  for (let i = 0; i < 12; i++) {
    const mid = (lo + hi) / 2;
    if (inGamut(mid)) lo = mid; else hi = mid;
  }
  return lo;
}

export function hexToRgb(hex) {
  const n = parseInt(hex.slice(1), 16);
  return [n >> 16 & 255, n >> 8 & 255, n & 255];
}

export function hexToHsl(hex) {
  const [r, g, b] = hexToRgb(hex).map(v => v / 255);
  const max = Math.max(r, g, b), min = Math.min(r, g, b);
  const l = (max + min) / 2;
  const d = max - min;
  if (d === 0) return [0, 0, l];
  const sat = d / (1 - Math.abs(2 * l - 1));
  let h;
  if (max === r) h = ((g - b) / d) % 6;
  else if (max === g) h = (b - r) / d + 2;
  else h = (r - g) / d + 4;
  return [(h * 60 + 360) % 360, sat, l];
}

// The accent roles on a light surface for OKLCH hue `h`, chroma `C` and secondary hue `h2`.
// The light-mode primary sits at tone 40 (about 0.47 in OKLCH) with white on it, which keeps accent
// text above 5:1 on white. `ac` is the main accent, `soft` the accent used for text and `strong`
// the fill behind button text.
function lightAccentRoles(h, C, h2) {
  const c = Math.min(0.2, C);
  const at = (hue, L, k = 1) => oklchToRgb(L, fitChroma(L, c * k, hue), hue);
  return {
    ac: at(h, 0.47).join(' '),
    soft: at(h, 0.33, 0.9).join(' '),
    strong: at(h, 0.47).join(' '),
    ink: '255 255 255',
    ac2: at(h2, 0.47).join(' '),
  };
}

// The same from HSL values (hue, saturation, secondary hue). The hue is read from a pastel of the
// colour, which is where OKLCH and HSL agree best.
function lightAccentRolesFromHsl(h1, s, h2) {
  const [, C, h] = rgbToOklch(hslToRgb(h1, s, 0.68));
  return lightAccentRoles(h, C, rgbToOklch(hslToRgb(h2, s, 0.68))[2]);
}

// `hues` is [accent hue, accent saturation, secondary hue, tint hue, tint saturation].
// Light keeps the tint weaker, since saturation reads stronger on pale surfaces, and uses darker
// accents so accent text stays readable.
export function paletteFromHues([h1, s1, h2, th, ts], mode = 'dark') {
  const light = mode === 'light';
  const s = Math.min(0.85, Math.max(0.4, s1));
  const warm = th >= 10 && th <= 60 ? 0.7 : 1;
  const t = Math.min(0.5, ts) * THEME_TINT * warm * (light ? 0.9 : 1);
  const surface = (l, k = 1) => hslToRgb(th, t * k, l).join(' ');
  const strong = hslToRgb(h1, s, 0.56);
  const surfaces = light ? {
    bg: surface(0.925), sunken: surface(0.875), chrome: surface(0.9), surface: surface(0.972), field: surface(0.99),
    raised: surface(0.9), raised2: surface(0.86), line: surface(0.82, 0.9), lineSoft: surface(0.87, 0.9),
    lineHover: surface(0.76, 0.9), lineStrong: surface(0.64, 0.9),
  } : {
    bg: surface(0.085), sunken: surface(0.065), chrome: surface(0.10), surface: surface(0.115), field: surface(0.07),
    raised: surface(0.155), raised2: surface(0.19), line: surface(0.21, 0.9), lineSoft: surface(0.16, 0.9),
    lineHover: surface(0.26, 0.9), lineStrong: surface(0.32, 0.9),
  };
  if (light) return { ...surfaces, ...lightAccentRolesFromHsl(h1, s, h2) };
  return {
    ...surfaces,
    ac: hslToRgb(h1, s, 0.62).join(' '),
    soft: brightEnough(h1, s, 0.76).join(' '),
    strong: strong.join(' '),
    ink: relativeLuminance(strong) < 0.2 ? '255 255 255' : '8 10 14',
    ac2: hslToRgb(h2, s, 0.62).join(' '),
  };
}

// Accent text on the dark surfaces: blues and purples are dark at the same HSL lightness, so lighten until it reads.
function brightEnough(h, s, lightness) {
  let rgb = hslToRgb(h, s, lightness);
  while (relativeLuminance(rgb) < 0.4 && lightness < 0.95) {
    lightness += 0.02;
    rgb = hslToRgb(h, s, lightness);
  }
  return rgb;
}

// The user's accent as one rule per theme; the light one is more specific, so it wins in light mode.
export function userAccentCss(color, tint) {
  const tintSat = ACCENT_TINTS[tint] ?? ACCENT_TINTS.subtle;
  if (color === DEFAULT_ACCENT && !tintSat) return '';
  const [h, sat, l] = hexToHsl(color);
  const hue = Math.round(h);
  // Progress bars sit on dark poster art in both themes, so they use the bright accent.
  const artAccent = color === DEFAULT_ACCENT
    ? STOCK_ACCENT.dark.ac
    : hslToRgb(h, Math.min(0.9, Math.max(0.4, sat)), Math.min(0.72, Math.max(0.55, l))).join(' ');
  const rule = (mode) => {
    const light = mode === 'light';
    const pal = paletteFromHues([hue, sat, (hue + 40) % 360, hue, tintSat], mode);
    let accent = STOCK_ACCENT[mode];
    if (color !== DEFAULT_ACCENT && light) {
      const [, seedChroma, seedHue] = rgbToOklch(hexToRgb(color));
      accent = lightAccentRoles(seedHue, seedChroma, (seedHue + 40) % 360);
    } else if (color !== DEFAULT_ACCENT) {
      const s = Math.min(0.9, Math.max(0.4, sat));
      const lightness = Math.min(0.72, Math.max(0.55, l));
      const strong = hslToRgb(h, s, lightness - 0.08);
      accent = {
        ac: hslToRgb(h, s, lightness).join(' '),
        soft: brightEnough(h, s, Math.min(0.88, lightness + 0.13)).join(' '),
        strong: strong.join(' '),
        ink: relativeLuminance(strong) < 0.2 ? '255 255 255' : '8 10 14',
        ac2: pal.ac2,
      };
    }
    const vars = {
      '--ac': accent.ac, '--ac-soft': accent.soft, '--ac-strong': accent.strong, '--ac-ink': accent.ink, '--ac2': accent.ac2, '--ac-art': artAccent,
    };
    if (tintSat) {
      Object.assign(vars, {
        '--bg': pal.bg, '--sunken': pal.sunken, '--chrome': pal.chrome, '--surface': pal.surface, '--field': pal.field,
        '--raised': pal.raised, '--raised2': pal.raised2, '--line': pal.line, '--line-soft': pal.lineSoft,
        '--line-hover': pal.lineHover, '--line-strong': pal.lineStrong,
      });
    }
    return (light ? ':root[data-theme="light"]' : ':root') + '{' + Object.keys(vars).map(k => k + ':' + vars[k]).join(';') + '}';
  };
  return rule('dark') + rule('light');
}

const SHOW_VARIABLES = {
  '--ac': 'ac', '--ac-soft': 'soft', '--ac-strong': 'strong', '--ac-ink': 'ink', '--ac2': 'ac2', '--ac-art': 'ac',
  '--bg': 'bg', '--sunken': 'sunken', '--chrome': 'chrome', '--surface': 'surface', '--field': 'field', '--raised': 'raised', '--raised2': 'raised2',
  '--line': 'line', '--line-soft': 'lineSoft', '--line-hover': 'lineHover', '--line-strong': 'lineStrong',
};

// With the tint off the surfaces keep the theme's own colours; only the accent comes from the art.
const ACCENT_ROLES = ['--ac', '--ac-soft', '--ac-strong', '--ac-ink', '--ac2', '--ac-art'];

// The page colours for a show: the hues come from its art, how strongly the surfaces take them from the user's tint.
export function showPaletteVars(hues, tint, mode) {
  const [h1, s1, h2, th] = hues;
  const strength = ACCENT_TINTS[tint] ?? ACCENT_TINTS.subtle;
  const pal = paletteFromHues([h1, s1, h2, th, strength], mode);
  const vars = {};
  Object.keys(SHOW_VARIABLES).forEach(name => {
    if (strength || ACCENT_ROLES.includes(name)) vars[name] = pal[SHOW_VARIABLES[name]];
  });
  return vars;
}
