// The top of the Shows page: the episodes that need a look across every show.
import { html, useState } from '../ui.js';
import { apiFetch } from '../api.js';
import { useResource } from '../store.js';
import { go } from '../router.js';
import { attention as attentionData } from '../data.js';
import { episodeLabel, localTime } from '../format.js';
import { Icon, Spinner } from './Icon.js';
import { Poster } from './ShowSummary.js';
import { toast } from './Toast.js';

const KIND_LABELS = { stalled: 'Stalled', failed: 'Failed', retrying: 'Retrying', missed: 'Missed', unfound: 'No feed' };
const KIND_TONES = { stalled: 'warn', failed: 'bad', retrying: 'warn', missed: 'warn', unfound: 'warn' };

function Action({ row, pending, run }) {
  const action = row.action;
  if (!action) return row.kind === 'retrying' && row.retry_after ? html`<span class="muted">next try ${localTime(row.retry_after)}</span>` : null;
  const key = row.keys[0];
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

// Failed, stalled, retrying and missed episodes of every show, each with the one action that helps, and the shows the user
// watched every aired episode of that no feed carries.
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
  const dismiss = async (keys) => {
    // A 404 means the row is already gone (a second click, or it was fixed meanwhile): nothing is left to close.
    try { await apiFetch('/api/attention/dismiss', { method: 'POST', json: { keys }, quiet: true }); } catch (err) { if (err.status !== 404) toast(err.message, 'error'); }
  };
  return html`<section class="stack-sm show-section">
    <div class="page-head"><div class="row"><h2 class="section-title">Needs attention</h2><span class="count-badge">${data.length}</span></div></div>
    <div class="card card-divided attention-card">
      ${data.map(row => html`<div class="attention-row" key=${row.keys[0]} role="link" tabindex="0" onClick=${() => go(row.show_id)} onKeyDown=${(e) => { if (e.key === 'Enter') go(row.show_id); }}>
        <div class="sr-poster"><${Poster} show=${{ id: row.show_id, title: row.show, cover: row.cover }} dim=${false} small /></div>
        <div class="attention-main">
          <div class="history-title"><span class="history-show">${row.show}</span><span class="ep tabular">Ep ${episodeLabel(row.episodes)}</span><span class=${`state ${KIND_TONES[row.kind]}`}>${KIND_LABELS[row.kind]}</span></div>
          <div class="attention-reason">${row.reason}</div>
        </div>
        <${Action} row=${row} pending=${pending} run=${run} />
        <button type="button" class="btn plain icon-only sm" aria-label="Dismiss" title="Hide from this list. The episode is left as it is, and a release that turns up later is still downloaded."
          onClick=${(e) => { e.stopPropagation(); dismiss(row.keys); }} onKeyDown=${(e) => e.stopPropagation()}><${Icon} name="x" small /></button>
      </div>`)}
    </div>
  </section>`;
}
