import { html, useMemo, useState } from '../ui.js';
import { apiFetch } from '../api.js';
import { useResource } from '../store.js';
import { go, useNow } from '../router.js';
import { shows as showsData } from '../data.js';
import { Seg } from '../components/Controls.js';
import { Icon } from '../components/Icon.js';
import { confirmDialog } from '../components/Modal.js';
import { toast } from '../components/Toast.js';
import { ShowCard, ShowRow } from '../components/ShowSummary.js';

const read = (key, fallback) => { try { return localStorage.getItem(key) || fallback; } catch (err) { return fallback; } };
const write = (key, value) => { try { localStorage.setItem(key, value); } catch (err) { /* only a convenience */ } };

function sortShows(list, mode, now) {
  const copy = [...list];
  copy.sort((a, b) => {
    if (mode === 'airing') {
      const at = (s) => (s.next_airing ? Date.parse(s.next_airing.at) : Infinity);
      const upcoming = (t) => t > now && t !== Infinity;
      const [ta, tb] = [at(a), at(b)];
      if (upcoming(ta) && !upcoming(tb)) return -1;
      if (!upcoming(ta) && upcoming(tb)) return 1;
      if (ta !== tb) return ta - tb;
    }
    if (mode === 'default') return a.id - b.id;
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
      ${list.length ? list.map(show => html`<${Item} key=${show.id} show=${show} now=${now} />`) : html`<div class="empty" style="grid-column:1/-1">${empty}</div>`}
    </div>
  </section>`;
}

export function Shows() {
  const { data, loading } = useResource(showsData);
  const [sort, setSort] = useState(read('show_sort_mode', 'airing'));
  const [view, setView] = useState(read('show_view_mode', 'grid') === 'list' ? 'list' : 'grid');
  const [swap, setSwap] = useState(0);
  const now = useNow();
  const minute = Math.floor(now / 60000);
  const groups = useMemo(() => {
    const list = data || [];
    const at = minute * 60000;
    return {
      releasing: sortShows(list.filter(s => s.section === 'releasing'), sort, at),
      planned: sortShows(list.filter(s => s.section === 'planned'), sort, at),
      completed: sortShows(list.filter(s => s.section === 'completed'), sort, at),
    };
  }, [data, sort, minute]);
  if (loading) return html`<div class="page"></div>`;

  const changeSort = (value) => { setSwap(n => n + 1); setSort(value); write('show_sort_mode', value); };
  const changeView = (value) => { setSwap(n => n + 1); setView(value); write('show_view_mode', value); };
  const autoDiscover = async () => {
    const ok = await confirmDialog({
      title: 'Reset every show to auto-discover?',
      body: html`<p>Hand-picked feeds are cleared. Shows locked to a feed they already downloaded from, and completed shows, are kept.</p>`,
      confirm: 'Reset',
    });
    if (!ok) return;
    try {
      const { released } = await apiFetch('/api/shows/auto-discover-all', { method: 'POST' });
      toast(released ? `${released} show${released === 1 ? '' : 's'} set to auto-discover.` : 'No show had a hand-picked feed.', 'success');
    } catch (err) { /* shown by apiFetch */ }
  };
  const tools = html`<div class="row wrap">
    <button type="button" class="btn sm" onClick=${() => go('add')} title="Add a show from your AniList list and pick the feed to download it from"><${Icon} name="plus" small /><span>Add show</span></button>
    <button type="button" class="btn sm" onClick=${autoDiscover} title="Reset every show to auto-discover. Hand-picked feeds are cleared; shows locked to a feed they downloaded from are kept.">Auto-discover</button>
    <${Seg} small label="Sort" value=${sort} onChange=${changeSort} options=${[['airing', 'Airing', 'clock', 'Soonest airing first'], ['title', 'A-Z', 'az', 'A to Z'], ['default', 'Default', 'list', 'Order added']]} />
    <${Seg} small icons label="View" value=${view} onChange=${changeView} options=${[['grid', 'Grid view', 'grid', 'Poster grid'], ['list', 'List view', 'list', 'Compact list']]} />
  </div>`;
  return html`<div class="page full">
    <${Section} title="Releasing" tone="" count=${groups.releasing.length} list=${groups.releasing} view=${view} swap=${swap} now=${now} empty="No currently releasing anime." tools=${tools} />
    ${groups.planned.length ? html`<${Section} title="Planned" tone="info" count=${groups.planned.length} list=${groups.planned} view=${view} swap=${swap} now=${now} />` : null}
    ${groups.completed.length ? html`<${Section} title="Completed" tone="done" count=${groups.completed.length} list=${groups.completed} view=${view} swap=${swap} now=${now} />` : null}
  </div>`;
}
