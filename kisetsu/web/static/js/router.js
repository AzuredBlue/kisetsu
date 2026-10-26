// Hash routing: #shows, #calendar, #history, #feeds, #logs, #settings and #show/<id>.
import { useEffect, useState } from './ui.js';
import { feedMatches, showPage } from './data.js';

export const TABS = ['shows', 'calendar', 'history', 'feeds', 'logs', 'settings'];

export function parseHash(hash = window.location.hash) {
  const value = hash.replace(/^#\/?/, '');
  const show = /^show\/(\d+)$/.exec(value);
  if (show) return { name: 'show', id: Number(show[1]), tab: null };
  return { name: TABS.includes(value) ? value : 'shows', id: null, tab: null };
}

const reduceMotion = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const CARD_POSTER = (id) => `.show-grid [data-poster-id="${id}"], .show-list [data-poster-id="${id}"]`;
const POSTER_NAME = 'show-poster';
let transition = null;
let returnScroll = 0;
let starting = false;
let returnTab = 'shows';

// Where Back and Escape go: the tab the show was opened from.
export const leaveTarget = () => returnTab;

// Resolves once `find()` returns an element, or after a short wait. Frames are not drawn while a view transition waits for this, so it polls on a timer.
function appear(find, limit = 250) {
  return new Promise(resolve => {
    const started = performance.now();
    const look = () => {
      const el = find();
      if (el || performance.now() - started > limit) resolve(el);
      else setTimeout(look, 4);
    };
    look();
  });
}

// Resolves when the element's picture can be drawn, or after a short wait; an undecoded picture is captured as an empty box.
function decoded(scope, limit = 300, loading = false) {
  const pictures = [...(scope ? scope.querySelectorAll('img') : [])].filter(img => img.src && (loading ? !img.complete || img.naturalWidth : img.complete && img.naturalWidth));
  return Promise.race([Promise.all(pictures.map(img => img.decode().catch(() => {}))), new Promise(done => setTimeout(done, limit))]);
}

// The poster frame on the show page carries the border and shadow, so it is the element that moves.
const visible = (el) => {
  const frame = el && (el.closest('.show-poster') || el);
  return frame && frame.getClientRects().length ? frame : null;
};

// Starts loading a show's page, so that opening it can start at once.
export function prefetch(id) {
  [showPage(id), feedMatches(id)].filter(r => r.data === null && !r.inflight).forEach(r => r.load());
}

// Opening or closing a show page morphs the poster between the card and the page while the content rises.
export async function go(target) {
  const hash = typeof target === 'number' ? `#show/${target}` : `#${target}`;
  if (window.location.hash === hash) return;
  const opening = typeof target === 'number' && parseHash().name !== 'show';
  const closing = typeof target !== 'number' && parseHash().name === 'show';
  if (!(opening || closing) || reduceMotion() || !document.startViewTransition) {
    window.location.hash = hash;
    return;
  }
  if (starting) return;
  starting = true;
  if (transition) transition.skipTransition();
  const id = opening ? target : parseHash().id;
  if (opening) {
    const pending = [showPage(id), feedMatches(id)].filter(r => r.data === null).map(r => r.inflight || r.load());
    if (pending.length) await Promise.race([Promise.all(pending), new Promise(done => setTimeout(done, 400))]);
  }
  const kind = opening ? 'open' : 'close';
  const root = document.documentElement;
  const toGrid = closing && target === 'shows';
  const from = opening || toGrid ? visible(document.querySelector(opening ? CARD_POSTER(id) : `.show-poster [data-poster-id="${id}"]`)) : null;
  let to = null;
  const scroller = document.querySelector('.content');
  if (opening) {
    returnScroll = scroller.scrollTop;
    returnTab = parseHash().name === 'calendar' ? 'calendar' : 'shows';
  }
  if (opening) await decoded(document.querySelector('.content'));
  if (from) from.style.viewTransitionName = POSTER_NAME;
  root.classList.add(`vt-${kind}`);
  const current = document.startViewTransition(async () => {
    if (from) from.style.viewTransitionName = '';
    window.location.hash = hash;
    if (toGrid) {
      await appear(() => document.querySelector(CARD_POSTER(id)));
      scroller.scrollTop = returnScroll;
    }
    to = from ? await appear(() => visible(document.querySelector(opening ? `.show-poster [data-poster-id="${id}"]` : CARD_POSTER(id)))) : null;
    if (to) {
      await decoded(to, opening ? 200 : 60, true);
      to.style.viewTransitionName = POSTER_NAME;
    } else await appear(() => document.querySelector('.content > *'), 120);
  });
  current.ready.catch(() => {});
  current.updateCallbackDone.catch(() => {});
  transition = current;
  starting = false;
  // The page has no key handler until it has drawn, so Escape is listened for here while it opens.
  const onKey = (e) => { if (e.key === 'Escape') go(returnTab); };
  if (opening) window.addEventListener('keydown', onKey);
  current.finished.catch(() => {}).then(() => {
    window.removeEventListener('keydown', onKey);
    root.classList.remove(`vt-${kind}`);
    if (transition !== current) return;
    transition = null;
    if (from) from.style.viewTransitionName = '';
    if (to) to.style.viewTransitionName = '';
  });
}

export function useRoute() {
  const [route, setRoute] = useState(parseHash());
  useEffect(() => {
    const fn = () => setRoute(parseHash());
    window.addEventListener('hashchange', fn);
    return () => window.removeEventListener('hashchange', fn);
  }, []);
  return route;
}

// One shared one-second clock, running only while something that shows a countdown is on screen.
const tickers = new Set();
let timer = null;
export function useNow() {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const fn = () => setNow(Date.now());
    tickers.add(fn);
    if (!timer) timer = setInterval(() => tickers.forEach(f => f()), 1000);
    return () => {
      tickers.delete(fn);
      if (!tickers.size) { clearInterval(timer); timer = null; }
    };
  }, []);
  return now;
}
