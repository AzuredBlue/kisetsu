// The colour along each poster's bottom edge, for the panel under it. It is read here, from the cover the page
// already shows (AniList's image host allows a page to read its pixels), so the server never downloads covers.
// Kept per cover URL in this browser; "" when the edge has no usable colour or could not be read.
import { useEffect, useState } from './ui.js';

const STORE_KEY = 'kisetsu.posterEdges.v1';
const STORE_LIMIT = 500;
// The bottom strip read, and the size it is reduced to first, as the server once did.
const STRIP = 0.08;
const SAMPLE_W = 96;
const SAMPLE_H = 12;
const MIN_WEIGHT = 0.02;

const known = new Map(load());
// Listeners for every edge, and for one cover's.
const listeners = new Set();
const listenersOf = new Map();
// Covers loaded and waiting to be read while the browser is idle, so a page full of new covers does not stall.
const queued = new Map();
let saving = null;

function load() {
  try { return Object.entries(JSON.parse(localStorage.getItem(STORE_KEY) || '{}')); } catch (err) { return []; }
}

function save() {
  try {
    const entries = [...known.entries()].slice(-STORE_LIMIT);
    localStorage.setItem(STORE_KEY, JSON.stringify(Object.fromEntries(entries)));
  } catch (err) { /* storage is a convenience: without it edges are read again next visit */ }
}

const idle = (fn) => (window.requestIdleCallback ? requestIdleCallback(fn, { timeout: 1000 }) : setTimeout(() => fn({ timeRemaining: () => 8 }), 16));

// The edge colour of a cover: "#rrggbb", "" for none, undefined while not read yet.
export function edgeOf(url) { return url ? known.get(url) : undefined; }

// Re-render when an edge is read: only that cover's when `url` is given, else any.
export function useEdges(url) {
  const [, rerender] = useState(0);
  useEffect(() => {
    const fn = () => rerender(n => n + 1);
    const set = url === undefined ? listeners : (listenersOf.get(url) || listenersOf.set(url, new Set()).get(url));
    set.add(fn);
    return () => {
      set.delete(fn);
      if (url !== undefined && !set.size) listenersOf.delete(url);
    };
  }, [url]);
}

// Read the edge of a loaded <img> once per cover URL.
export function readEdge(img, url) {
  if (!url || known.has(url) || queued.has(url)) return;
  queued.set(url, img);
  if (queued.size === 1) readQueued();
}

// One cover at a time, each when the browser is idle.
async function readQueued() {
  while (queued.size) {
    await new Promise(idle);
    const [url, img] = queued.entries().next().value;
    let color = '';
    try { color = (await edgeColor(img, url)) || ''; } catch (err) { /* a cover that may not be read (no CORS) has no edge */ }
    queued.delete(url);
    known.set(url, color);
    (listenersOf.get(url) || []).forEach(fn => fn());
    listeners.forEach(fn => fn());
  }
  if (!saving) saving = setTimeout(() => { saving = null; save(); }, 1000);
}

// The cover's file, from the browser's cache: decoding a file happens off the main thread, while drawing the
// <img> decoded the whole cover on it, several milliseconds a cover.
async function stripOf(img, url, top, sw, sh) {
  const w = img.naturalWidth, h = img.naturalHeight;
  const options = { resizeWidth: sw, resizeHeight: sh, resizeQuality: 'medium' };
  try {
    const answer = await fetch(img.currentSrc || url, { mode: 'cors', credentials: 'omit' });
    if (answer.ok) return await createImageBitmap(await answer.blob(), 0, top, w, h - top, options);
  } catch (err) { /* read from the <img> instead */ }
  return createImageBitmap(img, 0, top, w, h - top, options);
}

async function edgeColor(img, url) {
  const w = img.naturalWidth, h = img.naturalHeight;
  if (!w || !h) return null;
  const top = Math.floor(h * (1 - STRIP));
  const scale = Math.min(1, SAMPLE_W / w, SAMPLE_H / (h - top));
  const sw = Math.max(1, Math.round(w * scale)), sh = Math.max(1, Math.round((h - top) * scale));
  const canvas = document.createElement('canvas');
  canvas.width = sw; canvas.height = sh;
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  if (window.createImageBitmap) {
    const strip = await stripOf(img, url, top, sw, sh);
    ctx.drawImage(strip, 0, 0, sw, sh);
    strip.close();
  } else {
    ctx.drawImage(img, 0, top, w, h - top, 0, 0, sw, sh);
  }
  const data = ctx.getImageData(0, 0, sw, sh).data;
  // An OKLab average weighted by how colourful each pixel is; near-black and near-white pixels are left out.
  let total = 0, sl = 0, sa = 0, sb = 0;
  for (let i = 0; i < data.length; i += 4) {
    const [L, a, b] = oklab(data[i], data[i + 1], data[i + 2]);
    if (L < 0.10 || L > 0.95) continue;
    const weight = Math.hypot(a, b) + MIN_WEIGHT;
    total += weight; sl += weight * L; sa += weight * a; sb += weight * b;
  }
  return total ? oklabToHex(sl / total, sa / total, sb / total) : null;
}

const toLinear = (v) => (v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4);
const toSrgb = (v) => { v = Math.min(1, Math.max(0, v)); return v <= 0.0031308 ? v * 12.92 : 1.055 * v ** (1 / 2.4) - 0.055; };

function oklab(r, g, b) {
  const lr = toLinear(r / 255), lg = toLinear(g / 255), lb = toLinear(b / 255);
  const l = Math.cbrt(0.4122214708 * lr + 0.5363325363 * lg + 0.0514459929 * lb);
  const m = Math.cbrt(0.2119034982 * lr + 0.6806995451 * lg + 0.1073969566 * lb);
  const s = Math.cbrt(0.0883024619 * lr + 0.2817188376 * lg + 0.6299787005 * lb);
  return [
    0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
    1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
    0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s,
  ];
}

function oklabToHex(L, a, b) {
  const l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3;
  const m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3;
  const s = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3;
  const rgb = [
    4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
    -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
    -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s,
  ];
  return '#' + rgb.map(c => Math.round(toSrgb(c) * 255).toString(16).padStart(2, '0')).join('');
}
