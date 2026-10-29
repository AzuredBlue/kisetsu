// The episode ledger and the releases the feeds carry, as two lists of the show page.
import { html, useState } from '../ui.js';
import { apiFetch } from '../api.js';
import { relativeTime, localTime } from '../format.js';
import { Icon, Spinner } from './Icon.js';
import { toast } from './Toast.js';

const ROLE_LABELS = { locked: 'Locked', picked: 'Picked', assigned: 'Assigned', other: 'Not used by this show' };
const GROUPS = [['attention', 'Needs attention'], ['active', 'Downloading'], ['done', 'Completed'], ['waiting', 'Waiting'], ['skipped', 'Not downloading']];

const version = (v) => (v > 1 ? ` v${v}` : '');

// "airs Sat 5:00 PM" for an episode still to come, with the date once it is a week or more away.
function airsLabel(ep, now) {
  if (ep.status !== 'wanted' || !ep.air_at) return '';
  const at = new Date(ep.air_at);
  const ahead = at - now;
  if (isNaN(ahead) || ahead <= 0) return '';
  const when = ahead >= 6 * 86400000
    ? at.toLocaleDateString([], { month: 'short', day: 'numeric' })
    : at.toLocaleString([], { weekday: 'short', hour: 'numeric', minute: '2-digit' });
  return `airs ${when}`;
}

// What an episode row says and which group it belongs to.
export function describe(ep, { paused, now }) {
  const aired = ep.status === 'wanted' && ep.air_at && new Date(ep.air_at) <= now;
  const retryAt = ep.retry_after ? new Date(ep.retry_after) : null;
  const retrying = aired && retryAt && retryAt > now && ep.last_error;
  let label;
  let tone = '';
  if (ep.status === 'completed') { label = ep.release && ep.release.completed_at ? relativeTime(ep.release.completed_at, now) : 'completed'; tone = 'ok'; }
  else if (ep.status === 'failed') { label = 'failed'; tone = 'bad'; }
  else if (ep.status === 'missed') { label = 'missed'; tone = 'warn'; }
  else if (ep.status === 'skipped') label = 'skipped';
  else if (retrying) { label = `retry ${localTime(ep.retry_after)}`; tone = 'warn'; }
  else if (aired) { label = paused ? 'aired, paused' : 'aired, searching'; tone = paused ? '' : 'info'; }
  else if (ep.status === 'downloading') { label = ep.replacing ? 'replacing' : 'downloading'; tone = 'info'; }
  else label = airsLabel(ep, now) || ep.status;
  const group = ep.status === 'completed' ? 'done'
    : ep.status === 'skipped' ? 'skipped'
    : ep.status === 'failed' || ep.status === 'missed' || retrying ? 'attention'
    : ep.status === 'downloading' ? 'active' : 'waiting';
  return { label, tone, group };
}

function RowButton({ pending, onClick, title, icon = 'download' }) {
  return html`<button type="button" class="row-btn" disabled=${pending} onClick=${onClick} title=${title}>${pending ? html`<${Spinner} small />` : html`<${Icon} name=${icon} small />`}</button>`;
}

// Runs one request for a row; the button is held while it is out so a second click does nothing.
export function useRowAction(showId, done) {
  const [pending, setPending] = useState(new Set());
  const run = async (key, url, json, success) => {
    if (pending.has(key)) return;
    setPending(set => new Set(set).add(key));
    try {
      const data = await apiFetch(url, { method: 'POST', json, quiet: true });
      toast((data && data.message) || success, 'success');
      if (done) done();
    } catch (err) {
      toast(err.message, err.status === 409 || err.status === 400 ? 'info' : 'error');
    } finally {
      setPending(set => { const next = new Set(set); next.delete(key); return next; });
    }
  };
  return { pending, download: (title) => run(title, `/api/shows/${showId}/download`, { title }, 'Download started.'), restore: (episodeId) => run(`restore:${episodeId}`, `/api/shows/${showId}/episodes/${episodeId}/restore`, undefined, 'Restored.'), want: (episodeId) => run(`want:${episodeId}`, `/api/shows/${showId}/episodes/${episodeId}/want`, undefined, 'It will be downloaded.') };
}

export function EpisodeList({ episodes, groups, paused, now, actions, onSearch, searching }) {
  const usable = (groups || []).filter(g => g.usable).flatMap(g => g.matches);
  const replacements = new Map();
  usable.forEach(m => { const best = replacements.get(m.episode); if (m.replaces && (!best || m.version > best.version)) replacements.set(m.episode, m); });
  const rows = episodes.map(ep => ({ ep, ...describe(ep, { paused, now }) }));
  const missedAny = (group) => rows.some(r => r.group === group && r.ep.status === 'missed');
  const search = (group) => !missedAny(group) ? null : searching
    ? html`<span class="muted row"><${Spinner} small /> Searching…</span>`
    : html`<button type="button" class="link" onClick=${onSearch} title="Ask the feeds' sites for the episodes that are no longer in their RSS">Search older</button>`;
  const body = GROUPS.map(([name, title]) => {
    const entries = rows.filter(r => r.group === name).sort((a, b) => b.ep.number - a.ep.number);
    if (!entries.length) return null;
    return html`<li class="list-head" key=${`g:${name}`}><span>${title}</span><span class="muted tabular">${entries.length}</span><span class="push">${search(name)}</span></li>
      ${entries.map(({ ep, label, tone }) => {
        const release = ep.release;
        const unmanaged = ep.status === 'completed' && release && !release.tracked;
        const retry = ep.status === 'failed' ? usable.find(m => m.episode === ep.number && m.downloadable) : null;
        const swap = ep.status === 'completed' && release && release.tracked ? replacements.get(ep.number) : null;
        return html`<li class="list-row" key=${`e:${ep.id}`}>
          <span class="ep-no">Ep ${ep.number}${release ? version(release.version) : ''}</span>
          <span class="release-name mono truncate" title=${release ? release.title : ''}>${release ? release.title : ''}</span>
          <span class="row-end">
            <span class=${`state ${tone}`} title=${ep.last_error || undefined}>${label}${unmanaged ? ', unmanaged' : ''}</span>
            ${retry ? html`<${RowButton} pending=${actions.pending.has(retry.title)} onClick=${() => actions.download(retry.title)} title="Try this release again" />` : null}
            ${ep.status === 'skipped' ? html`<${RowButton} pending=${actions.pending.has(`want:${ep.id}`)} onClick=${() => actions.want(ep.id)} title="Download this episode after all" />` : null}
            ${unmanaged ? html`<${RowButton} pending=${actions.pending.has(`restore:${ep.id}`)} onClick=${() => actions.restore(ep.id)} title="Restore: find this torrent in qBittorrent and manage it, or add it again from the feed." />` : null}
            ${swap ? html`<${RowButton} icon="swap" pending=${actions.pending.has(swap.title)} onClick=${() => actions.download(swap.title)} title=${`Replace Ep ${ep.number} with this release. The current copy${release.feed ? ` from ${release.feed}` : ''} is deleted with its files once this one is seeding.`} />` : null}
          </span>
        </li>`;
      })}`;
  });
  return html`<ul class="list">${body.some(Boolean) ? body : html`<li class="list-empty">No episode records yet.</li>`}</ul>`;
}

export function ReleaseList({ groups, actions, loading, onUseFeed }) {
  if (loading) return html`<ul class="list"><li class="list-empty">Checking the feed…</li></ul>`;
  const any = (groups || []).some(g => g.matches.length);
  return html`<ul class="list">
    ${any ? groups.map(g => html`<li class="list-head" key=${`h:${g.feed_id}`}>
        <span class="truncate">${g.feed_name}</span>
        ${ROLE_LABELS[g.role] ? html`<span class="muted">${ROLE_LABELS[g.role]}</span>` : null}
        <span class="push muted tabular">${g.matches.length}</span>
        ${g.role === 'other' ? html`<button type="button" class="link" onClick=${() => onUseFeed(g.feed_id)} title=${`Move this show to ${g.feed_name}`}>Use this feed</button>` : null}
      </li>
      ${g.matches.map(m => html`<li class="list-row" key=${`f:${g.feed_id}:${m.title}`}>
        <span class="ep-no">Ep ${m.episode}${version(m.version)}</span>
        <span class="release-name mono truncate" title=${m.display_title && m.display_title !== m.title ? `${m.display_title}\n${m.title}` : m.title}>${m.display_title || m.title}</span>
        <span class="row-end wide">
          ${m.downloadable
            ? html`<${RowButton} icon=${m.replaces ? 'swap' : 'download'} pending=${actions.pending.has(m.title)} onClick=${() => actions.download(m.title)}
                title=${m.replaces ? `Replace Ep ${m.episode} with this release. The current copy is deleted with its files once this one is seeding.` : m.restore ? 'Restore: add this release to qBittorrent, or tag the copy already there, so Kisetsu manages it.' : 'Download this release now. It is tracked like any other episode.'} />`
            : html`<span class="muted">${m.episode_status}</span>`}
        </span>
      </li>`)}`)
      : html`<li class="list-empty">Nothing in any feed matches this show right now. Try Search older.</li>`}
  </ul>`;
}
