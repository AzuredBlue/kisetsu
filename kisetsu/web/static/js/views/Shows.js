import { html, useEffect, useMemo, useRef, useState } from '../ui.js';
import { useResource } from '../store.js';
import { go, useNow } from '../router.js';
import { shows as showsData } from '../data.js';
import { Seg } from '../components/Controls.js';
import { Icon, Spinner } from '../components/Icon.js';
import { LoadError } from '../components/LoadError.js';
import { ShowCard, ShowRow } from '../components/ShowSummary.js';
import { Checklist, NeedsAttention } from '../components/Attention.js';
import { EmptyState } from '../components/EmptyState.js';

const read = (key, fallback) => { try { return localStorage.getItem(key) || fallback; } catch (err) { return fallback; } };
const write = (key, value) => { try { localStorage.setItem(key, value); } catch (err) { /* only a convenience */ } };

// The filter outlives the page, so coming back from a show finds the same list at the same place.
let lastQuery = '';

const matches = (show, needle) => [show.title, show.title_romaji, show.title_english, ...(show.custom_aliases || [])]
  .some(name => name && name.toLowerCase().includes(needle));

// `/` focuses the filter, unless the key is being typed somewhere or a dialog is open.
function useSlashFocus(ref) {
  useEffect(() => {
    const onKey = (e) => {
      if (e.key !== '/' || e.ctrlKey || e.metaKey || e.altKey || document.querySelector('.modal')) return;
      const el = e.target;
      if (el && (el.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName))) return;
      e.preventDefault();
      if (ref.current) ref.current.focus();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, []);
}

// Soonest airing first, then A to Z; whatever has nothing coming up goes last.
function sortShows(list, now) {
  const copy = [...list];
  copy.sort((a, b) => {
    const at = (s) => (s.next_airing ? Date.parse(s.next_airing.at) : Infinity);
    const upcoming = (t) => t > now && t !== Infinity;
    const [ta, tb] = [at(a), at(b)];
    if (upcoming(ta) && !upcoming(tb)) return -1;
    if (!upcoming(ta) && upcoming(tb)) return 1;
    if (ta !== tb) return ta - tb;
    return a.title.localeCompare(b.title);
  });
  return copy;
}

function Section({ title, count, tone, list, view, now, empty, tools, swap }) {
  const Item = view === 'list' ? ShowRow : ShowCard;
  return html`<section class="stack-sm show-section">
    <div class="page-head">
      <div class="row"><h2 class="section-title">${title}</h2><span class=${`count-badge ${tone}`}>${count}</span></div>
      ${tools}
    </div>
    <div key=${swap} class=${`${view === 'list' ? 'show-list' : 'show-grid'}${swap ? ' show-swap' : ''}`}>
      ${list.length ? list.map(show => html`<${Item} key=${show.id} show=${show} now=${now} />`) : html`<div style="grid-column:1/-1">${empty}</div>`}
    </div>
  </section>`;
}

export function Shows() {
  const { data, loading, error, reload } = useResource(showsData);
  const [view, setView] = useState(read('show_view_mode', 'grid') === 'list' ? 'list' : 'grid');
  const [swap, setSwap] = useState(0);
  const [query, setQuery] = useState(lastQuery);
  const filter = useRef(null);
  useSlashFocus(filter);
  const now = useNow();
  const minute = Math.floor(now / 60000);
  const groups = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const list = (data || []).filter(show => !needle || matches(show, needle));
    const at = minute * 60000;
    return {
      releasing: sortShows(list.filter(s => s.section === 'releasing'), at),
      planned: sortShows(list.filter(s => s.section === 'planned'), at),
      completed: sortShows(list.filter(s => s.section === 'completed'), at),
    };
  }, [data, minute, query]);
  if (loading) return html`<div class="page"><div class="empty"><${Spinner} /></div></div>`;
  if (!data) return html`<div class="page"><${LoadError} what="your shows" error=${error} reload=${reload} /></div>`;

  const changeView = (value) => { setSwap(n => n + 1); setView(value); write('show_view_mode', value); };
  const search = (value) => { lastQuery = value; setQuery(value); };
  const onFilterKey = (e) => {
    if (e.key !== 'Escape') return;
    if (query) search(''); else e.target.blur();
  };
  const filtering = !!query.trim();
  const tools = html`<div class="row wrap">
    <div class="add-search shows-filter">
      <${Icon} name="search" small />
      <input ref=${filter} class="field" type="search" placeholder="Filter shows" aria-label="Filter shows" title="Press / to filter" value=${query} onInput=${(e) => search(e.target.value)} onKeyDown=${onFilterKey} />
    </div>
    <button type="button" class="btn sm primary" onClick=${() => go('add')} title="Add a show from your AniList list and pick the feed to download it from"><${Icon} name="plus" small /><span>Add show</span></button>
    <${Seg} small icons label="View" value=${view} onChange=${changeView} options=${[['grid', 'Grid view', 'grid', 'Poster grid'], ['list', 'List view', 'list', 'Compact list']]} />
  </div>`;
  return html`<div class="page full">
    <${Checklist} />
    <${NeedsAttention} />
    <${Section} title="Releasing" tone="" count=${groups.releasing.length} list=${groups.releasing} view=${view} swap=${swap} now=${now} empty=${filtering ? html`<${EmptyState} title=${`No releasing show matches "${query.trim()}"`} />` : html`<${EmptyState} title="Nothing airing right now" hint="Shows on your AniList Watching list that air this season appear here after a sync. Use Add show for anything else." />`} tools=${tools} />
    ${groups.completed.length ? html`<${Section} title="Completed" tone="done" count=${groups.completed.length} list=${groups.completed} view=${view} swap=${swap} now=${now} />` : null}
    ${groups.planned.length ? html`<${Section} title="Planned" tone="info" count=${groups.planned.length} list=${groups.planned} view=${view} swap=${swap} now=${now} />` : null}
  </div>`;
}
