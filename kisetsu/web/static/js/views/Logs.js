import { html, useEffect, useRef } from '../ui.js';
import { useResource } from '../store.js';
import { logs as logsData } from '../data.js';
import { localTime } from '../format.js';
import { Icon } from '../components/Icon.js';
import { toast } from '../components/Toast.js';

const MARKS = { ERROR: ['ERROR', 'bad'], WARNING: ['WARN', 'warn'], DEBUG: ['DEBUG', 'info'] };

export function Logs() {
  const { data, reload } = useResource(logsData);
  const box = useRef(null);
  useEffect(() => { if (box.current) box.current.scrollTop = box.current.scrollHeight; }, [data]);
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
        <button type="button" class="btn sm" onClick=${reload}><${Icon} name="refresh" small />Refresh</button>
        <button type="button" class="btn sm" onClick=${copy}><${Icon} name="copy" small />Copy</button>
      </div>
    </div>
    <div class="log-pane">
      <div class="log-head muted">${data ? data.length : 0} entries</div>
      <div class="log-lines mono" ref=${box}>
        ${!data || !data.length ? html`<div class="empty">No activity logged yet.</div>` : data.map((l, i) => {
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
