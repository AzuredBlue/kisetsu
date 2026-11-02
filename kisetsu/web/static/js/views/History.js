import { html, useMemo } from '../ui.js';
import { apiFetch } from '../api.js';
import { useResource } from '../store.js';
import { useNow } from '../router.js';
import { history as historyData } from '../data.js';
import { dayLabel, localTime, relativeTime } from '../format.js';
import { Icon } from '../components/Icon.js';
import { LoadError } from '../components/LoadError.js';
import { confirmDialog } from '../components/Modal.js';
import { Poster, posterVars } from '../components/ShowSummary.js';
import { useTheme } from '../theme.js';
import { toast } from '../components/Toast.js';
import { go } from '../router.js';

// A finished download is the normal case, so its word stays quiet and only its icon carries the colour.
const KINDS = { grabbed: ['Queued', null], done: ['Downloaded', 'check'], replaced: ['Replaced', 'swap'], failed: ['Failed', null], missed: ['Missed', null] };
// A failure or a miss is told by its reason, not by a release name (a missed episode has none).
const PROBLEMS = ['failed', 'missed'];

// Episodes added as one batch come from one release, so they share a row: only the number differs.
// Failures and misses share one only when the reason is the same too, since their title may be empty.
function collapseBatches(items) {
  const out = [];
  items.forEach(item => {
    const last = out[out.length - 1];
    const same = last && last.show_id === item.show_id && last.kind === item.kind && last.feed === item.feed && last.title === item.title && (!PROBLEMS.includes(item.kind) || last.message === item.message);
    if (same && item.episode && last.episodes.length) last.episodes.push(item.episode);
    else out.push({ ...item, episodes: item.episode ? [item.episode] : [] });
  });
  return out;
}

// [10, 9, 8, 3] -> "3, 8–10"
function episodeLabel(numbers) {
  const runs = [];
  [...numbers].sort((a, b) => a - b).forEach(n => {
    const run = runs[runs.length - 1];
    if (run && run[1] === n - 1) run[1] = n; else runs.push([n, n]);
  });
  return runs.map(([a, b]) => (a === b ? a : `${a}–${b}`)).join(', ');
}

function Row({ item, now }) {
  const [verb, icon] = KINDS[item.kind] || [item.kind, null];
  const title = item.show || item.title || 'Unknown show';
  const problem = PROBLEMS.includes(item.kind);
  const { resolved } = useTheme();
  const poster = html`<div class="sr-poster"><${Poster} show=${{ id: item.show_id, title, cover: item.cover }} dim=${false} /></div>`;
  return html`<div class="history-row" style=${posterVars(item, resolved)}>
    ${item.show_id ? html`<a href=${`#show/${item.show_id}`} tabindex="-1" aria-hidden="true">${poster}</a>` : poster}
    <div class="history-main">
      <div class="history-title">
        ${item.show_id ? html`<a class="history-show" href=${`#show/${item.show_id}`} title=${title}>${title}</a>` : html`<span class="history-show" title=${title}>${title}</span>`}
        ${item.episodes.length ? html`<span class="ep tabular">Ep ${episodeLabel(item.episodes)}</span>` : null}
      </div>
      ${problem && item.message ? html`<div class=${`history-reason ${item.title ? 'clamp-1' : 'clamp-2'}`} title=${item.message}>${item.message}</div>` : null}
      ${item.title ? html`<div class=${`release-name mono ${problem && item.message ? 'clamp-1' : 'clamp-2'}`} title=${item.title}>${item.title}</div>` : null}
    </div>
    <div class="history-meta">
      <span class=${`history-status ${item.kind}`}>${icon ? html`<${Icon} name=${icon} small />` : null}${verb}</span>
      <span class="history-feed truncate" title=${item.feed}>${item.feed}</span>
      <span class="history-time tabular" title=${item.at ? `${new Date(item.at).toLocaleString()} (${relativeTime(item.at, now)})` : ''}>${localTime(item.at)}</span>
    </div>
  </div>`;
}

export function History() {
  const { data, error, reload } = useResource(historyData);
  const now = useNow();
  const minute = Math.floor(now / 60000);
  const groups = useMemo(() => {
    const out = [];
    (data || []).forEach(item => {
      const label = dayLabel(item.at);
      const last = out[out.length - 1];
      if (last && last.label === label) last.items.push(item); else out.push({ label, items: [item] });
    });
    return out.map(group => ({ ...group, items: collapseBatches(group.items) }));
  }, [data, minute]);
  const clear = async () => {
    if (!(await confirmDialog({ title: 'Clear the history?', body: html`<p>The list of downloads is emptied. Downloads and shows are not touched.</p>`, confirm: 'Clear', danger: true }))) return;
    try { await apiFetch('/api/history', { method: 'DELETE' }); toast('History cleared.', 'success'); } catch (err) { /* shown by apiFetch */ }
  };
  return html`<div class="page narrow history">
    <div class="page-head">
      <p class="page-sub">Episodes Kisetsu has queued, downloaded or replaced, and the ones that failed or never turned up.</p>
      <button type="button" class="btn sm plain" onClick=${clear} disabled=${!data || !data.length}>Clear</button>
    </div>
    ${!data ? (error ? html`<${LoadError} what="the history" error=${error} reload=${reload} />` : null) : !data.length
      ? html`<div class="card empty"><div>Nothing here yet</div><p class="field-hint">Episodes show up here once Kisetsu queues them.</p></div>`
      : groups.map(group => html`<section key=${group.label}>
          <div class="section-title history-day">${group.label}</div>
          <div class="card card-divided history-card">${group.items.map(item => html`<${Row} key=${item.id} item=${item} now=${now} />`)}</div>
        </section>`)}
  </div>`;
}
