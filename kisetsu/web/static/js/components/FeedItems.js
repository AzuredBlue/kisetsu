// What the feeds have brought in, newest first, a page at a time: all feeds or one, only releases for followed shows or everything.
import { html, useEffect, useRef, useState } from '../ui.js';
import { useResource } from '../store.js';
import { useNow } from '../router.js';
import { feedItems } from '../data.js';
import { relativeTime } from '../format.js';
import { Seg } from './Controls.js';
import { feedHealth } from './FeedsTable.js';

const STACKED_PAGE = 10;
const FALLBACK_ROW = 57;
const WIDE = '(min-width: 1100px)';
const KEY = 'kisetsu.feedItems.mine';
const saved = () => { try { return localStorage.getItem(KEY) !== '0'; } catch (err) { return true; } };
const save = (on) => { try { localStorage.setItem(KEY, on ? '1' : '0'); } catch (err) { /* only a convenience */ } };

const query = (feedId, mine, size, page) => `${feedId === null ? '' : `feed_id=${feedId}&`}matched=${mine}&limit=${size}&offset=${page * size}`;

// The feed choice.
function FeedPills({ feeds, selected, onSelect }) {
  return html`<div class="chips" role="group" aria-label="Feed">
    <button type="button" class="chip" aria-pressed=${String(selected === null)} onClick=${() => onSelect(null)}>All feeds</button>
    ${feeds.map(feed => html`<button type="button" class="chip" key=${feed.id} aria-pressed=${String(selected === feed.id)} onClick=${() => onSelect(feed.id)}>${feed.name}</button>`)}
  </div>`;
}

function Status({ feeds, selected, recent }) {
  const feed = selected === null ? null : feeds.find(f => f.id === selected);
  const failing = feed ? (feed.failing_since ? feed : null) : feeds.find(f => f.failing_since);
  const lastRead = feed ? feed.last_ok_at : feeds.reduce((best, f) => (f.last_ok_at && (!best || f.last_ok_at > best) ? f.last_ok_at : best), null);
  return html`<div class="row wrap feed-status">
    ${failing ? feedHealth(failing) : lastRead ? feedHealth({ last_ok_at: lastRead }) : html`<span class="muted">Not read yet</span>`}
    ${recent ? html`<span class="muted">${recent} new in the last day</span>` : null}
  </div>`;
}

function Row({ item, now, showFeed }) {
  const at = item.published_at || item.first_seen_at;
  return html`<div class="item-row">
    <div class="grow">
      <div class="release-name mono truncate" title=${item.title}>${item.title}</div>
      <div class="item-meta">
        ${item.shows.map(show => html`<a key=${show.id} class="item-show truncate" href=${`#show/${show.id}`}>${show.title}</a>`)}
        ${showFeed ? html`<span class="muted truncate">from ${item.feed}</span>` : null}
      </div>
    </div>
    <div class="item-when">
      ${item.download ? html`<span class=${`verb${item.download === 'Downloaded' ? ' done' : item.download === 'Downloading' ? ' busy' : ''}`}>${item.download}</span>` : null}
      <span class="muted" title=${new Date(at).toLocaleString()}>${relativeTime(at, now)}</span>
    </div>
  </div>`;
}

// Always seven slots (first, last and the pages around the current one), so the buttons keep their place from page to page.
// In the middle the current page stays in the centre slot, so the next click lands where the pointer already is.
// Buttons are keyed by slot, not page: the highlighted one stays the same element and only its number changes.
function slots(page, pages) {
  if (pages <= 7) return Array.from({ length: pages }, (_, n) => n);
  if (page <= 3) return [0, 1, 2, 3, 4, 'gap-end', pages - 1];
  if (page >= pages - 4) return [0, 'gap-start', pages - 5, pages - 4, pages - 3, pages - 2, pages - 1];
  return [0, 'gap-start', page - 1, page, page + 1, 'gap-end', pages - 1];
}

function Pager({ page, pages, onPage }) {
  return html`<nav class="pager" aria-label="Pages">
    <button type="button" disabled=${page === 0} onClick=${() => onPage(page - 1)}>Previous</button>
    ${slots(page, pages).map((slot, at) => typeof slot === 'string'
      ? html`<span class="pager-gap" key=${at}>…</span>`
      : html`<button type="button" key=${at} aria-current=${slot === page ? 'page' : undefined} onClick=${() => onPage(slot)}>${slot + 1}</button>`)}
    <button type="button" disabled=${page >= pages - 1} onClick=${() => onPage(page + 1)}>Next</button>
  </nav>`;
}

export function FeedItems({ feeds, selected, onSelect }) {
  const [mine, setMine] = useState(saved);
  const [wide, setWide] = useState(() => window.matchMedia(WIDE).matches);
  const [fit, setFit] = useState(() => Math.max(3, Math.floor((window.innerHeight - 330) / FALLBACK_ROW)));
  const [at, setAt] = useState({ key: '', page: 0 });
  const list = useRef(null);
  const last = useRef(null);
  const now = useNow();
  useEffect(() => {
    const media = window.matchMedia(WIDE);
    const change = () => setWide(media.matches);
    media.addEventListener('change', change);
    return () => media.removeEventListener('change', change);
  }, []);
  // As many rows as fit above the page numbers, so the page itself never scrolls.
  useEffect(() => {
    const el = list.current;
    if (!el || !wide) return undefined;
    const measure = () => {
      const row = el.querySelector('.item-row');
      const rowHeight = row ? parseFloat(getComputedStyle(row).minHeight) + 1 : FALLBACK_ROW;
      setFit(Math.max(3, Math.floor(el.clientHeight / rowHeight)));
    };
    measure();
    const watch = new ResizeObserver(measure);
    watch.observe(el);
    return () => watch.disconnect();
  }, [wide]);
  const size = wide ? fit : STACKED_PAGE;
  const key = `${selected}|${mine}|${size}`;
  const page = at.key === key ? at.page : 0;
  const { data } = useResource(feedItems(query(selected, mine, size, page)));
  if (data) last.current = data;
  const shown = data || last.current;
  const pages = shown ? Math.max(1, Math.ceil(shown.total / size)) : 1;
  const go = (n) => setAt({ key, page: Math.min(Math.max(n, 0), pages - 1) });
  return html`<div class="feed-items">
    <${FeedPills} feeds=${feeds} selected=${selected} onSelect=${onSelect} />
    <div class="feed-sub">
      <${Seg} small label="Which releases" value=${mine ? 'mine' : 'all'} onChange=${(v) => { setMine(v === 'mine'); save(v === 'mine'); }} options=${[['mine', 'For my shows'], ['all', 'Everything']]} />
      <${Status} feeds=${feeds} selected=${selected} recent=${shown ? shown.new_recent : 0} />
    </div>
    <div class="card card-divided item-list" ref=${list}>
      ${!shown ? null : !shown.items.length
        ? html`<div class="empty"><div>${mine ? 'No releases for your shows yet' : 'Nothing read yet'}</div><p class="field-hint">${mine ? 'Releases that match a show you follow show up here.' : 'Articles show up here as soon as a feed has been read.'}</p></div>`
        : shown.items.map(item => html`<${Row} key=${item.id} item=${item} now=${now} showFeed=${selected === null} />`)}
    </div>
    <${Pager} page=${page} pages=${pages} onPage=${go} />
  </div>`;
}
