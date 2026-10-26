// Theme (light, dark or follow the OS) is kept in this browser; the accent colour is a setting on the server.
import { useEffect, useState } from './ui.js';
import { DEFAULT_ACCENT, showPaletteVars, userAccentCss } from './color.js';

const THEME_KEY = 'kisetsu_theme';
const ACCENT_CSS_KEY = 'kisetsu_accent_css4';
const listeners = new Set();

export function themeMode() {
  try { return localStorage.getItem(THEME_KEY) || 'dark'; } catch (err) { return 'dark'; }
}

export function resolvedTheme() {
  const mode = themeMode();
  return mode === 'light' || (mode === 'system' && window.matchMedia('(prefers-color-scheme: light)').matches) ? 'light' : 'dark';
}

export function applyTheme() {
  document.documentElement.setAttribute('data-theme', resolvedTheme());
  paintShow();
  listeners.forEach(fn => fn());
}

export function setThemeMode(mode) {
  try { localStorage.setItem(THEME_KEY, mode); } catch (err) { /* private window */ }
  applyTheme();
}

export function toggleTheme() {
  setThemeMode(resolvedTheme() === 'light' ? 'dark' : 'light');
}

window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', () => {
  if (themeMode() === 'system') applyTheme();
});

let accent = { color: DEFAULT_ACCENT, tint: 'subtle' };
export function currentAccent() { return accent; }

export function applyUserAccent(color, tint, { persist = true } = {}) {
  accent = { color, tint };
  const css = userAccentCss(color, tint);
  let el = document.getElementById('user-accent');
  if (!el) {
    el = document.createElement('style');
    el.id = 'user-accent';
    document.head.appendChild(el);
  }
  el.textContent = css;
  // Last in <head>, so it beats the stylesheets' own :root colours.
  document.head.appendChild(el);
  if (persist) {
    try {
      if (css) localStorage.setItem(ACCENT_CSS_KEY, css);
      else localStorage.removeItem(ACCENT_CSS_KEY);
    } catch (err) { /* only avoids a flash on load */ }
  }
  paintShow();
  listeners.forEach(fn => fn());
}

// While a show's page is open its colours replace the page's: set inline, so they beat the user's accent rules.
let showHues = null;
let painted = [];
function paintShow() {
  const root = document.documentElement;
  painted.forEach(name => root.style.removeProperty(name));
  const vars = showHues ? showPaletteVars(showHues, accent.tint, resolvedTheme()) : {};
  painted = Object.keys(vars);
  painted.forEach(name => root.style.setProperty(name, vars[name]));
}

export function applyShowPalette(hues) {
  showHues = hues && hues.length >= 4 ? hues : null;
  paintShow();
}

export function useTheme() {
  const [, rerender] = useState(0);
  useEffect(() => {
    const fn = () => rerender(n => n + 1);
    listeners.add(fn);
    return () => listeners.delete(fn);
  }, []);
  return { mode: themeMode(), resolved: resolvedTheme(), accent };
}
