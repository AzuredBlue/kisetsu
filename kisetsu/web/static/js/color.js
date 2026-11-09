// Accent and surface colours. Everything is an "r g b" triple string, so CSS can add alpha with rgb(var(--ac) / .5).
// Colours are worked out in OKLCH: a colour keeps its hue as it lightens or darkens, and equal L reads as equally light,
// so every accent and tint can be held to the same contrast with fixed lightness steps.

export const DEFAULT_ACCENT = '#2dd4bf';
// A saved accent is one of these hexes or a custom colour. It is the seed: the colours drawn come from its hue and chroma.
export const ACCENT_PRESETS = [
  ['Teal', '#2dd4bf'], ['Sky', '#38bdf8'], ['Indigo', '#818cf8'], ['Violet', '#a78bfa'], ['Pink', '#f472b6'],
  ['Rose', '#fb7185'], ['Orange', '#fb923c'], ['Amber', '#fbbf24'], ['Lime', '#a3e635'], ['Emerald', '#34d399'],
];
// The OKLCH chroma the surfaces and greys take from the tint hue (0 keeps them neutral).
export const ACCENT_TINTS = { off: 0, subtle: 0.016, full: 0.034 };

// Surface and text lightness per theme. Every text level holds 5.5:1 on every surface it can sit on, at any tint.
// --line-field, a control's edge, is not held to 3:1: the control's own fill, its label and its focus ring mark it.
const NEUTRAL_L = {
  dark: {
    sunken: 0.155, bg: 0.178, field: 0.165, chrome: 0.195, surface: 0.212, raised: 0.262, raised2: 0.300,
    lineSoft: 0.268, line: 0.320, lineHover: 0.390, lineStrong: 0.550, lineField: 0.420,
    text: 0.955, text2: 0.830, text3: 0.730, textBtn: 0.880, textSeg: 0.740, btn: 0.262, btnHover: 0.300,
  },
  light: {
    sunken: 0.920, bg: 0.962, field: 0.995, chrome: 0.945, surface: 0.993, raised: 0.935, raised2: 0.900,
    lineSoft: 0.905, line: 0.865, lineHover: 0.780, lineStrong: 0.580, lineField: 0.710,
    text: 0.220, text2: 0.360, text3: 0.440, textBtn: 0.300, textSeg: 0.420, btn: 0.900, btnHover: 0.865,
  },
};
const NEUTRAL_VARS = {
  sunken: ['--sunken'], bg: ['--bg'], field: ['--field'], chrome: ['--chrome'], surface: ['--surface'], raised: ['--raised'],
  raised2: ['--raised2'], lineSoft: ['--line-soft'], line: ['--line'], lineHover: ['--line-hover'], lineStrong: ['--line-strong'],
  lineField: ['--line-field'], text: ['--text'], text2: ['--text-2'], text3: ['--text-3', '--text-4'], textBtn: ['--text-btn'], textSeg: ['--text-seg'],
  btn: ['--btn'], btnHover: ['--btn-h'],
};

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

const oklch = (L, C, h) => oklchToRgb(L, fitChroma(L, C, h), h).join(' ');

export function hexToRgb(hex) {
  const n = parseInt(hex.slice(1), 16);
  return [n >> 16 & 255, n >> 8 & 255, n & 255];
}

// The page greys for tint hue `h` (OKLCH) and chroma. Light pages take less of the hue, since it reads stronger on
// pale surfaces, and warm hues less again, or they turn cream. Text takes half the chroma, lines a little less, a
// control's edge a little more, and secondary buttons three times as much (up to .045, past which they compete with
// the primary button and the selected controls), so with any tint on they read as tonal fills of the accent.
function neutrals(h, chroma, mode) {
  const light = mode === 'light';
  let c = light ? chroma * 0.6 : chroma;
  if (light && h >= 50 && h <= 110) c *= 0.6;
  const steps = NEUTRAL_L[light ? 'light' : 'dark'];
  const out = {};
  Object.keys(steps).forEach(key => {
    const btn = key.startsWith('btn');
    const k = key.startsWith('text') ? 0.5 : key === 'lineField' ? 1.3 : key.startsWith('line') ? 0.9 : btn ? 3 : 1;
    out[key] = oklch(steps[key], btn ? Math.min(c * k, 0.045) : c * k, h);
  });
  return out;
}

// The accent roles for a seed colour. Dark: the seed keeps its own lightness inside .68-.80, so each preset keeps its
// character (yellows stay lighter than blues) while all clear the same contrast, with dark ink on the fill. Light: one
// lightness for all, with white ink. `ac` is the main accent, `soft` the accent used for text, `strong` a filled
// button at rest (it brightens to `ac` on hover), `art` the bright accent drawn over poster art in both themes.
function accentRoles(seed, mode) {
  const [L0, C0, h] = rgbToOklch(seed);
  const L = Math.min(0.80, Math.max(0.68, L0));
  const art = oklch(L, C0, h);
  if (mode === 'light') {
    const c = Math.min(0.2, C0);
    return { ac: oklch(0.50, c, h), soft: oklch(0.40, c * 0.9, h), strong: oklch(0.44, c, h), ink: '255 255 255', ac2: oklch(0.50, c, (h + 40) % 360), art };
  }
  return {
    ac: art, soft: oklch(Math.min(0.92, L + 0.10), C0 * 0.8, h), strong: oklch(L - 0.06, C0, h),
    ink: oklch(0.18, 0.01, h), ac2: oklch(L, C0, (h + 40) % 360), art,
  };
}

// A swatch in the accent's bright colour, the same in both themes: the darker light-theme accents of neighbouring
// presets (teal and emerald, pink and rose) are too alike to choose between.
export function swatchColor(hex) {
  return `rgb(${accentRoles(hexToRgb(hex), 'dark').art})`;
}

const ACCENT_VARS = { ac: '--ac', soft: '--ac-soft', strong: '--ac-strong', ink: '--ac-ink', ac2: '--ac2', art: '--ac-art' };

function toVars(roles, neutral) {
  const vars = {};
  Object.keys(ACCENT_VARS).forEach(key => { vars[ACCENT_VARS[key]] = roles[key]; });
  if (neutral) Object.keys(neutral).forEach(key => NEUTRAL_VARS[key].forEach(name => { vars[name] = neutral[key]; }));
  return vars;
}

// The OKLCH hue of an HSL hue, read from a mid colour of it, where the two agree best.
const hslHue = (h, s = 0.5) => rgbToOklch(hslToRgb(h, s, 0.6))[2];

// A show's colours from its art: `hues` is [accent hue, accent saturation, secondary hue, tint hue, ...] in HSL.
// The surfaces take the tint hue at `chroma`; with 0 they stay neutral.
export function paletteFromHues([h1, s1, , th], mode = 'dark', chroma = 0) {
  const roles = accentRoles(hslToRgb(h1, Math.min(0.85, Math.max(0.4, s1)), 0.62), mode);
  return { ...neutrals(hslHue(th), chroma, mode), ...roles };
}

// A poster's AniList colour as title text: its hue at one OKLCH lightness per theme, so every hue
// reads equally bright (above 5:1 on the page). Near-grey posters have no hue to show and get null.
export function posterTextColor(hex, mode = 'dark') {
  if (!/^#[0-9a-f]{6}$/i.test(hex || '')) return null;
  const [, C, h] = rgbToOklch(hexToRgb(hex));
  if (C < 0.03) return null;
  const L = mode === 'light' ? 0.45 : 0.8;
  return oklchToRgb(L, fitChroma(L, Math.min(0.17, Math.max(0.08, C)), h), h).join(' ');
}

// The colours a poster's bottom edge lends its card on hover: the edge's hue at one OKLCH lightness per
// theme, so the panel under the poster carries the art on and the title on it reads. A near-grey edge
// keeps its grey panel but has no hue worth giving the title, so its text stays null.
export function posterEdgeColors(hex, mode = 'dark') {
  if (!/^#[0-9a-f]{6}$/i.test(hex || '')) return null;
  const [, C, h] = rgbToOklch(hexToRgb(hex));
  const light = mode === 'light';
  const [panelL, panelC, textL] = light ? [0.93, 0.04, 0.40] : [0.30, 0.08, 0.84];
  const color = (L, c) => oklchToRgb(L, fitChroma(L, Math.min(c, C), h), h).join(' ');
  return { panel: color(panelL, panelC), text: C < 0.02 ? null : color(textL, 0.14) };
}

// The user's accent as one rule per theme; the light one is more specific, so it wins in light mode.
// The default accent with no tint is exactly what tokens.css holds, so it needs no rule.
export function userAccentCss(color, tint) {
  const chroma = ACCENT_TINTS[tint] ?? ACCENT_TINTS.subtle;
  if (color === DEFAULT_ACCENT && !chroma) return '';
  const seed = hexToRgb(color);
  const [, seedChroma, hue] = rgbToOklch(seed);
  // A grey accent has no hue to lend the surfaces.
  const tintChroma = seedChroma < 0.02 ? 0 : chroma;
  const rule = (mode) => {
    const vars = toVars(accentRoles(seed, mode), tintChroma ? neutrals(hue, tintChroma, mode) : null);
    return (mode === 'light' ? ':root[data-theme="light"]' : ':root') + '{' + Object.keys(vars).map(k => k + ':' + vars[k]).join(';') + '}';
  };
  return rule('dark') + rule('light');
}

// The page colours for a show: the hues come from its art, how strongly the surfaces take them from the user's tint.
// With the tint off the surfaces keep the theme's own colours; only the accent comes from the art.
export function showPaletteVars(hues, tint, mode) {
  const chroma = ACCENT_TINTS[tint] ?? ACCENT_TINTS.subtle;
  const pal = paletteFromHues(hues, mode, chroma);
  const neutral = {};
  if (chroma) Object.keys(NEUTRAL_VARS).forEach(key => { neutral[key] = pal[key]; });
  // On its own page the show's accent is drawn over the page as well as the art.
  return toVars({ ...pal, art: pal.ac }, chroma ? neutral : null);
}
