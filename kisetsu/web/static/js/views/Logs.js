import { html, useEffect, useRef } from '../ui.js';
import { useResource } from '../store.js';
import { apiFetch } from '../api.js';
import { logs as logsData, requests as requestsData, status as statusData } from '../data.js';
import { localTime } from '../format.js';
import { Icon } from '../components/Icon.js';
import { LoadError } from '../components/LoadError.js';
import { EmptyState } from '../components/EmptyState.js';
import { toast } from '../components/Toast.js';

const MARKS = { ERROR: ['ERROR', 'bad'], WARNING: ['WARN', 'warn'], DEBUG: ['DEBUG', 'info'] };
const REFRESH_MS = 5000;

// How many requests went to each site, by kind, since start or the last reset: to see what costs requests.
function Requests() {
  const { data, reload } = useResource(requestsData);
  // Feed reads write no log line, so the counts are refetched on a timer while the page is open.
  useEffect(() => { const id = setInterval(reload, REFRESH_MS); return () => clearInterval(id); }, []);
  if (!data) return null;
  const reset = async (e) => {
    e.preventDefault();
    try { await apiFetch('/api/requests', { method: 'DELETE' }); reload(); } catch (err) { /* shown by apiFetch */ }
  };
  const minutes = Math.max(1, (Date.now() - Date.parse(data.since)) / 60000);
  return html`<details class="card requests-card">
    <summary class="row"><b class="tabular">${data.total}</b> requests sent since ${localTime(data.since, true)}<span class="muted tabular">(${(data.total / minutes).toFixed(1)}/min)</span>
      <button type="button" class="btn sm plain" onClick=${reset}>Reset</button></summary>
    ${data.rows.length ? html`<table class="requests-table tabular">
      <thead><tr><th>Kind</th><th>Site</th><th class="num">Requests</th><th class="num">Rate-limited</th><th class="num">Failed</th></tr></thead>
      <tbody>${data.rows.map(r => html`<tr key=${`${r.kind}|${r.host}`}>
        <td>${r.kind}</td><td class="mono">${r.host}</td><td class="num">${r.requests}</td>
        <td class=${`num ${r.rate_limited ? 'warn' : 'muted'}`}>${r.rate_limited}</td><td class=${`num ${r.error ? 'bad' : 'muted'}`}>${r.error}</td>
      </tr>`)}</tbody>
    </table>` : html`<p class="muted">None yet.</p>`}
  </details>`;
}

export function Logs() {
  const { data, error, reload } = useResource(logsData);
  // The request counts are for measuring in the sandbox; the normal app does not show them.
  const { data: info } = useResource(statusData);
  const box = useRef(null);
  const atBottom = useRef(true);
  useEffect(() => { if (box.current && atBottom.current) box.current.scrollTop = box.current.scrollHeight; }, [data]);
  const onScroll = (e) => { const el = e.currentTarget; atBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24; };
  const copy = async () => {
    if (!data || !data.length) { toast('No logs to copy.', 'info'); return; }
    try {
      await navigator.clipboard.writeText(data.map(l => `[${localTime(l.at, true)}] [${l.level}] ${l.message}`).join('\n'));
      toast('Copied.', 'success');
    } catch (err) { toast('Failed to copy logs.', 'error'); }
  };
  return html`<div class="page wide">
    <div class="page-head">
      <p class="page-sub">What Kisetsu has been doing. New lines appear by themselves.</p>
      <div class="row">
        <button type="button" class="btn sm" onClick=${copy}><${Icon} name="copy" small />Copy</button>
      </div>
    </div>
    ${info && info.sandbox ? html`<${Requests} />` : null}
    <div class="log-pane">
      <div class="log-head muted">${data ? data.length : 0} entries</div>
      <div class="log-lines mono" ref=${box} onScroll=${onScroll}>
        ${!data && error ? html`<${LoadError} what="the logs" error=${error} reload=${reload} />` : !data || !data.length ? html`<${EmptyState} title="No activity logged yet" hint="What Kisetsu does is written here as it happens." />` : data.map((l, i) => {
          const mark = MARKS[l.level];
          return html`<div class="log-line" key=${`${l.at}${i}`}>
            <span class="log-time">${localTime(l.at, true)}</span>
            <span class="log-msg">${mark ? html`<b class=${mark[1]}>${mark[0]} </b>` : null}${l.message}</span>
          </div>`;
        })}
      </div>
    </div>
  </div>`;
}
