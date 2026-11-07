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
const listeners = new Set();

function load() {
  try { return Object.entries(JSON.parse(localStorage.getItem(STORE_KEY) || '{}')); } catch (err) { return []; }
}

function save() {
  try {
    const entries = [...known.entries()].slice(-STORE_LIMIT);
    localStorage.setItem(STORE_KEY, JSON.stringify(Object.fromEntries(entries)));
  } catch (err) { /* storage is a convenience: without it edges are read again next visit */ }
}

// The edge colour of a cover: "#rrggbb", "" for none, undefined while not read yet.
export function edgeOf(url) { return url ? known.get(url) : undefined; }

// Re-render when an edge is read.
export function useEdges() {
  const [, rerender] = useState(0);
  useEffect(() => {
    const fn = () => rerender(n => n + 1);
    listeners.add(fn);
    return () => listeners.delete(fn);
  }, []);
}

// Read the edge of a loaded <img> once per cover URL.
export function readEdge(img, url) {
  if (!url || known.has(url)) return;
  let color = '';
  try { color = edgeColor(img) || ''; } catch (err) { /* a cover that may not be read (no CORS) has no edge */ }
  known.set(url, color);
  save();
  listeners.forEach(fn => fn());
}

function edgeColor(img) {
  const w = img.naturalWidth, h = img.naturalHeight;
  if (!w || !h) return null;
  const top = Math.floor(h * (1 - STRIP));
  const scale = Math.min(1, SAMPLE_W / w, SAMPLE_H / (h - top));
  const sw = Math.max(1, Math.round(w * scale)), sh = Math.max(1, Math.round((h - top) * scale));
  const canvas = document.createElement('canvas');
  canvas.width = sw; canvas.height = sh;
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  ctx.drawImage(img, 0, top, w, h - top, 0, 0, sw, sh);
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
