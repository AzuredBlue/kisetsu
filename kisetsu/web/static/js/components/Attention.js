// The top of the Shows page: what a new user still has to set up, and the episodes that need a look across every show.
import { html, useState } from '../ui.js';
import { apiFetch } from '../api.js';
import { useResource } from '../store.js';
import { go } from '../router.js';
import { attention as attentionData, status as statusData } from '../data.js';
import { localTime, relativeTime } from '../format.js';
import { Icon, Spinner } from './Icon.js';
import { Poster } from './ShowSummary.js';
import { toast } from './Toast.js';

// Until the first download has happened: each thing Kisetsu needs, done or not, with where to fix it.
export function Checklist() {
  const { data: info } = useResource(statusData);
  const [syncing, setSyncing] = useState(false);
  if (!info || info.any_download) return null;
  const sync = async () => {
    setSyncing(true);
    try { await apiFetch('/api/refresh', { method: 'POST' }); toast('Reading your AniList list and the feeds.', 'success'); } catch (err) { /* shown by apiFetch */ }
    setSyncing(false);
  };
  const steps = [
    {
      // Connected only once startup has read qBittorrent; until then no error does not mean it answered.
      done: info.ready && !info.qbit_error, title: 'qBittorrent connected',
      todo: info.qbit_error
        ? html`Kisetsu can't reach qBittorrent. <a class="link" href="#settings">Check the connection in Settings</a>.`
        : html`Connecting to qBittorrent…`,
    },
    {
      done: info.feeds > 0, title: `${info.feeds} RSS feed${info.feeds === 1 ? '' : 's'}`,
      todo: html`No RSS feeds yet, so nothing can be found. <a class="link" href="#feeds">Add a feed</a>.`,
    },
    {
      done: !!info.anilist_synced_at, title: info.anilist_synced_at ? `AniList read ${relativeTime(info.anilist_synced_at, Date.now())}` : '',
      todo: html`Your AniList list hasn't been read yet. <button type="button" class="link" onClick=${sync} disabled=${syncing}>${syncing ? 'Syncing…' : 'Sync now'}</button>`,
    },
    {
      done: false, title: '',
      todo: html`Waiting for the first release. Each show is looked for in the feeds as its episodes air.`,
    },
  ];
  return html`<section class="card checklist" aria-label="Getting started">
    <h2 class="section-title">Getting started</h2>
    <ul>${steps.map((step, i) => html`<li key=${i} class=${step.done ? 'ok' : 'todo'}>
      <${Icon} name=${step.done ? 'check' : 'info'} small />
      <span>${step.done ? step.title : step.todo}</span>
    </li>`)}</ul>
  </section>`;
}

const KIND_LABELS = { stalled: 'Stalled', failed: 'Failed', retrying: 'Retrying', missed: 'Missed' };
const KIND_TONES = { stalled: 'warn', failed: 'bad', retrying: 'warn', missed: 'warn' };

function Action({ row, pending, run }) {
  const action = row.action;
  if (!action) return row.kind === 'retrying' && row.retry_after ? html`<span class="muted">next try ${localTime(row.retry_after)}</span>` : null;
  const key = `${row.episode_id}`;
  const busy = pending === key;
  const label = { download: 'Try again', try_another: 'Try another release', search: 'Search older' }[action.type];
  const request = {
    download: () => apiFetch(`/api/shows/${row.show_id}/download`, { method: 'POST', json: { title: action.title }, quiet: true }),
    try_another: () => apiFetch(`/api/shows/${row.show_id}/episodes/${row.episode_id}/try-another`, { method: 'POST', quiet: true }),
    search: () => apiFetch(`/api/shows/${row.show_id}/search-releases`, { method: 'POST', quiet: true }),
  }[action.type];
  return html`<button type="button" class="btn sm" disabled=${busy} onClick=${(e) => { e.stopPropagation(); run(key, request); }}>
    ${busy ? html`<${Spinner} small />` : null}<span>${label}</span>
  </button>`;
}

// Failed, stalled, retrying and missed episodes of every show, each with the one action that helps.
export function NeedsAttention() {
  const { data } = useResource(attentionData);
  const [pending, setPending] = useState(null);
  if (!data || !data.length) return null;
  const run = async (key, request) => {
    setPending(key);
    try {
      const body = await request();
      toast((body && body.message) || (body && body.found !== undefined ? (body.found ? `Found ${body.found} release${body.found === 1 ? '' : 's'}.` : body.missing === 0 ? 'No episode is missing, so there was nothing to search for.' : 'Nothing older was found.') : 'Done.'), 'success');
    } catch (err) { toast(err.message, err.status === 400 || err.status === 409 ? 'info' : 'error'); }
    setPending(null);
  };
  return html`<section class="stack-sm show-section">
    <div class="page-head"><div class="row"><h2 class="section-title">Needs attention</h2><span class="count-badge">${data.length}</span></div></div>
    <div class="card card-divided attention-card">
      ${data.map(row => html`<div class="attention-row" key=${`${row.episode_id}`} role="link" tabindex="0" onClick=${() => go(row.show_id)} onKeyDown=${(e) => { if (e.key === 'Enter') go(row.show_id); }}>
        <div class="sr-poster"><${Poster} show=${{ id: row.show_id, title: row.show, cover: row.cover }} dim=${false} /></div>
        <div class="attention-main">
          <div class="history-title"><span class="history-show">${row.show}</span><span class="ep tabular">Ep ${row.episode}</span><span class=${`state ${KIND_TONES[row.kind]}`}>${KIND_LABELS[row.kind]}</span></div>
          <div class="attention-reason">${row.reason}</div>
        </div>
        <${Action} row=${row} pending=${pending} run=${run} />
      </div>`)}
    </div>
  </section>`;
}
