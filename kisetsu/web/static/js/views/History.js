import { html, useMemo } from '../ui.js';
import { apiFetch } from '../api.js';
import { useResource } from '../store.js';
import { useNow } from '../router.js';
import { history as historyData } from '../data.js';
import { dayLabel, relativeTime } from '../format.js';
import { confirmDialog } from '../components/Modal.js';
import { toast } from '../components/Toast.js';
import { go } from '../router.js';

const VERBS = { grabbed: ['Queued', 'queued'], done: ['Downloaded', 'done'], replaced: ['Replaced', 'replaced'] };

function Row({ item, now }) {
  const [verb, tone] = VERBS[item.kind] || [item.kind, ''];
  return html`<div class="history-row">
    <div class="grow">
      <div class="row" style="align-items:baseline;min-width:0">
        ${item.show_id ? html`<a class="history-show truncate" href=${`#show/${item.show_id}`}>${item.show || item.title}</a>` : html`<span class="history-show truncate">${item.show || item.title}</span>`}
        ${item.episode ? html`<span class="ep tabular">Ep ${item.episode}</span>` : null}
      </div>
      <div class="release-name mono truncate" title=${item.title}>${item.title}</div>
    </div>
    <div class="history-when">
      <div class="muted" title=${item.at ? new Date(item.at).toLocaleString() : ''}>${relativeTime(item.at, now)}</div>
      <div class="muted truncate"><span class=${`verb ${tone}`}>${verb}</span>${item.feed ? ` from ${item.feed}` : ''}</div>
    </div>
  </div>`;
}

export function History() {
  const { data } = useResource(historyData);
  const now = useNow();
  const minute = Math.floor(now / 60000);
  const groups = useMemo(() => {
    const out = [];
    (data || []).forEach(item => {
      const label = dayLabel(item.at);
      const last = out[out.length - 1];
      if (last && last.label === label) last.items.push(item); else out.push({ label, items: [item] });
    });
    return out;
  }, [data, minute]);
  const clear = async () => {
    if (!(await confirmDialog({ title: 'Clear the history?', body: html`<p>The list of downloads is emptied. Downloads and shows are not touched.</p>`, confirm: 'Clear', danger: true }))) return;
    try { await apiFetch('/api/history', { method: 'DELETE' }); toast('History cleared.', 'success'); } catch (err) { /* shown by apiFetch */ }
  };
  return html`<div class="page narrow">
    <div class="page-head">
      <p class="page-sub">Episodes Kisetsu has queued, downloaded or replaced.</p>
      <div class="row">
        <button type="button" class="btn sm" onClick=${() => historyData.load()}>Refresh</button>
        <button type="button" class="btn sm" onClick=${clear} disabled=${!data || !data.length}>Clear</button>
      </div>
    </div>
    ${!data ? null : !data.length
      ? html`<div class="card empty"><div>Nothing here yet</div><p class="field-hint">Episodes show up here once Kisetsu queues them.</p></div>`
      : groups.map(group => html`<section key=${group.label}>
          <div class="history-day">${group.label}</div>
          <div class="card card-divided">${group.items.map(item => html`<${Row} key=${item.id} item=${item} now=${now} />`)}</div>
        </section>`)}
  </div>`;
}
