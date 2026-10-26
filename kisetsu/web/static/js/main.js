import { html, render, useEffect, useMemo, useState } from './ui.js';
import { setErrorHandler, apiFetch } from './api.js';
import { connect, useResource } from './store.js';
import { applyTheme, applyUserAccent, toggleTheme, resolvedTheme, useTheme } from './theme.js';
import { go, useNow, useRoute } from './router.js';
import { wait } from './format.js';
import { history, settings, setupStatus, shows, status } from './data.js';
import { Icon, Spinner } from './components/Icon.js';
import { ModalHost } from './components/Modal.js';
import { toast, Toasts } from './components/Toast.js';
import { Shows } from './views/Shows.js';
import { Show } from './views/Show.js';
import { Calendar, buildWeek } from './views/Calendar.js';
import { History } from './views/History.js';
import { Feeds } from './views/Feeds.js';
import { Logs } from './views/Logs.js';
import { Settings } from './views/Settings.js';
import { Setup } from './views/Setup.js';

const NAV = [
  ['shows', 'Shows', 'shows'],
  ['calendar', 'Calendar', 'calendar'],
  ['history', 'History', 'history'],
  ['feeds', 'RSS Feeds', 'feeds'],
  ['logs', 'Logs', 'logs'],
  ['settings', 'Settings', 'settings'],
];
const TITLES = { ...Object.fromEntries(NAV.map(([id, label]) => [id, label])), calendar: 'Weekly Calendar', history: 'Match History' };
const CHECK_JOBS = ['startup', 'refresh_feeds', 'sync_anilist', 'backfill'];

function Notes({ info }) {
  if (!info) return null;
  const offline = info.offline;
  const failed = info.episodes ? info.episodes.failed : 0;
  const testing = info.by_status ? info.by_status.testing || 0 : 0;
  const anilist = info.anilist_blocked_until && Date.parse(info.anilist_blocked_until) > Date.now();
  return html`<div class="notes">
    ${offline ? html`<span class="note-bad" title="Kisetsu did not answer the last check. Trying again.">Not responding</span>` : null}
    ${info.all_paused ? html`<span class="note-warn">Paused</span>` : null}
    ${info.qbit_error ? html`<span class="note-bad" title=${info.qbit_error}>qBittorrent unreachable</span>` : null}
    ${testing ? html`<span class="note-warn" title="No release seen yet. Trying the feeds by priority.">${testing} Testing</span>` : null}
    ${failed ? html`<span class="note-bad" title="Episodes that could not be downloaded. Open the show to see why.">${failed} Failed</span>` : null}
    ${anilist ? html`<span class="note-warn" title="AniList is limiting requests or not answering. Downloads carry on meanwhile.">AniList paused</span>` : null}
  </div>`;
}

function NextCheck({ info, at }) {
  const now = useNow();
  if (!info) return html`<span>…</span>`;
  const running = (info.running || []).some(name => CHECK_JOBS.includes(name));
  if (running) return html`<span>Checking…</span>`;
  if (info.next_feed_read_in === null || info.next_feed_read_in === undefined) return html`<span>Routine check</span>`;
  const left = info.next_feed_read_in - (now - at) / 1000;
  return html`<span>${left <= 0 ? 'Checking…' : wait(left)}</span>`;
}

function Sidebar({ route, open, onClose, info, at }) {
  useTheme();
  const [busy, setBusy] = useState(false);
  const showList = useResource(shows).data;
  const rows = useResource(history).data;
  const week = useMemo(() => (showList ? buildWeek(showList, new Date()).days.reduce((n, day) => n + day.slots.reduce((m, slot) => m + slot.items.length, 0), 0) : null), [showList]);
  const running = (info && (info.running || []).some(name => CHECK_JOBS.includes(name))) || busy;
  const paused = info && info.all_paused;
  const sync = async () => {
    setBusy(true);
    try { await apiFetch('/api/refresh', { method: 'POST' }); } catch (err) { /* shown by apiFetch */ }
    setBusy(false);
  };
  const togglePause = async () => {
    try {
      await apiFetch(paused ? '/api/shows/resume-all' : '/api/shows/pause-all', { method: 'POST' });
      toast(paused ? 'Downloading resumed.' : 'Downloading paused. Torrents already added carry on.', 'success');
    } catch (err) { /* shown by apiFetch */ }
  };
  const badge = (id) => {
    if (!info) return null;
    if (id === 'shows') return info.shows;
    if (id === 'feeds') return null;
    return null;
  };
  const light = resolvedTheme() === 'light';
  return html`
    ${open ? html`<div class="backdrop" onClick=${onClose}></div>` : null}
    <aside class=${`sidebar${open ? ' open' : ''}`}>
      <div class="brand">
        <span>Kisetsu</span>
        <button type="button" class="btn plain icon-only sm" onClick=${toggleTheme} title=${light ? 'Switch to dark theme' : 'Switch to light theme'} aria-label=${light ? 'Switch to dark theme' : 'Switch to light theme'}>
          <${Icon} name=${light ? 'moon' : 'sun'} />
        </button>
      </div>
      <nav>
        ${NAV.map(([id, label, icon]) => html`<button type="button" class="nav-item" key=${id} aria-current=${route.name === id || (id === 'shows' && route.name === 'show') ? 'page' : undefined} onClick=${() => { go(id); onClose(); }}>
          <${Icon} name=${icon} /><span>${label}</span>
          ${badge(id) !== null && badge(id) !== undefined ? html`<span class="badge">${badge(id)}</span>` : null}
        </button>`)}
      </nav>
      <div class="foot">
        <div class="next-check"><span>Next check</span><${NextCheck} info=${info} at=${at} /></div>
        <div class="row">
          <button type="button" class="btn sm" style="flex:1" onClick=${sync} disabled=${running} title="Re-sync AniList, read the RSS feeds, grab new episodes and reconcile with qBittorrent">
            ${running ? html`<${Spinner} small />` : null}<span>Sync Now</span>
          </button>
          <button type="button" class="btn sm icon-only" onClick=${togglePause} title=${paused ? 'Resume downloading' : 'Pause downloading'} aria-label=${paused ? 'Resume downloading' : 'Pause downloading'}>
            <${Icon} name=${paused ? 'play' : 'pause'} small />
          </button>
        </div>
      </div>
    </aside>`;
}

function Shell() {
  const route = useRoute();
  const [menu, setMenu] = useState(false);
  const live = useResource(status);
  const online = useResource(settings);
  const info = live.data ? { ...live.data, offline: !!live.error } : null;
  useEffect(() => {
    window.scrollTo(0, 0);
  }, [route.name]);
  useEffect(() => {
    if (online.data) applyUserAccent(online.data.accent_color, online.data.accent_tint);
  }, [online.data && online.data.accent_color, online.data && online.data.accent_tint]);
  const View = { shows: Shows, show: Show, calendar: Calendar, history: History, feeds: Feeds, logs: Logs, settings: Settings }[route.name];
  const title = route.name === 'show' ? 'Shows' : TITLES[route.name];
  return html`
    <${Sidebar} route=${route} open=${menu} onClose=${() => setMenu(false)} info=${info} at=${live.at} />
    <main>
      <header class="topbar">
        <div class="row" style="min-width:0">
          <button type="button" class="btn plain icon-only menu-btn" onClick=${() => setMenu(true)} aria-label="Open menu"><${Icon} name="menu" /></button>
          <h1 class="truncate">${title}</h1>
        </div>
        <${Notes} info=${info} />
      </header>
      <div class=${`content${route.name === 'show' ? ' flush' : ''}`} id="content"><${View} key=${route.name === 'show' ? `show-${route.id}` : route.name} route=${route} /></div>
    </main>`;
}

function App() {
  const setup = useResource(setupStatus);
  const [done, setDone] = useState(false);
  if (!setup.data) return html`<div class="empty" style="margin:auto">${setup.error ? 'Kisetsu did not answer.' : ''}</div>`;
  return html`${setup.data.setup_completed && !done ? html`<${Shell} />` : html`<${Setup} sandbox=${setup.data.sandbox} done=${done} setDone=${setDone} />`}<${ModalHost} /><${Toasts} />`;
}

setErrorHandler((message) => toast(message, 'error'));
applyTheme();
connect();
render(html`<${App} />`, document.getElementById('app'));
