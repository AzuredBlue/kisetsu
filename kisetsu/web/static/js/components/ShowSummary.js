// The pieces every place that shows a show is built from: poster, progress, air line, tags and problems.
import { html, useState } from '../ui.js';
import { apiFetch } from '../api.js';
import { countdown, initials, localDateTime, plural, progressFraction } from '../format.js';
import { go, prefetch } from '../router.js';
import { Icon } from './Icon.js';
import { confirmDialog } from './Modal.js';
import { toast } from './Toast.js';

const TAGS = {
  testing: ['pill-warn', 'No release seen yet. Trying the feeds by priority.'],
  waiting: ['pill-info', 'Aired, but no release has turned up yet. It is grabbed as soon as a feed carries it.'],
  paused: ['pill-idle', 'Monitoring is paused for this show.'],
};

// "Ep n" plus a countdown or outcome.
export function airSummary(show, now) {
  if (show.status === 'completed') {
    return { ep: show.progress.total ? `${show.progress.total} eps` : '', info: 'Completed', tone: 'muted', at: null };
  }
  const next = show.next_airing;
  if (next && next.at) {
    const cd = countdown(next.at, now);
    if (cd === 'Aired' && show.last_done && show.last_done >= next.episode) {
      return { ep: `Ep ${show.last_done}`, info: 'Downloaded', tone: 'ok', at: null };
    }
    return { ep: `Ep ${next.episode}`, info: cd === 'Aired' ? 'Aired' : `in ${cd}`, tone: cd === 'Aired' && show.status !== 'waiting' ? 'warn' : '', at: next.at };
  }
  if (show.last_done) return { ep: `Ep ${show.last_done}`, info: '', tone: '', at: null };
  return { ep: '', info: '', tone: '', at: null };
}

export function dimmed(show) {
  return show.paused || show.status === 'completed';
}

// A tag only says what the section and the countdown don't already say: Working, Upcoming and Completed get none.
export function StatusTag({ show }) {
  const tag = TAGS[show.status];
  return tag ? html`<span class=${`pill ${tag[0]}`} title=${tag[1]}>${show.status_label}</span>` : null;
}

// The cover, or the show's initials when it has none or it does not load.
export function Poster({ show, zoom = false, dim: dimmedOverride }) {
  const [broken, setBroken] = useState(false);
  const dim = (dimmedOverride === undefined ? dimmed(show) : dimmedOverride) ? ' dim' : '';
  const art = show.cover && !broken
    ? html`<img src=${show.cover} alt=${show.title} loading="lazy" class=${dim.trim()} onError=${() => setBroken(true)} />`
    : html`<div class=${`no-art${dim}`}>${initials(show.title)}</div>`;
  return html`<div class="poster on-art" data-poster-id=${show.id}>${zoom ? html`<div class="poster-zoom">${art}</div>` : art}</div>`;
}

export function ProgressBar({ show }) {
  const { done, aired, total } = show.progress;
  const known = total > 0 || done > 0 || !!show.next_airing;
  const pct = known ? Math.round(progressFraction(show.progress) * 100) : 0;
  return html`<div class="bar" title=${`${done}/${total || '?'} downloaded`}><div class=${dimmed(show) ? 'dim' : ''} style=${`width:${pct}%`}></div></div>`;
}

export function countText(show) {
  const { done, total } = show.progress;
  return `${done}/${total || '?'}`;
}

// Only what needs the user's eye, in plain words.
export function Problems({ show }) {
  const { failed, missed } = show.counts;
  if (!failed && !missed) return null;
  return html`<div class="problems">
    ${failed ? html`<span class="bad" title=${`${plural(failed, 'episode')} could not be downloaded. Open the show to see why.`}>${failed} failed</span>` : null}
    ${missed ? html`<span class="warn" title=${`${plural(missed, 'aired episode')} not found in any feed.`}>${missed} missed</span>` : null}
  </div>`;
}

export async function togglePause(show) {
  try {
    await apiFetch(`/api/shows/${show.id}/${show.paused ? 'resume' : 'pause'}`, { method: 'POST' });
    toast(show.paused ? `Monitoring resumed for ${show.title}.` : `Monitoring paused for ${show.title}. Torrents already added carry on.`, 'success');
  } catch (err) { /* shown by apiFetch */ }
}

function torrentList(preview) {
  if (!Array.isArray(preview)) return html`<p class="field-hint">The list of torrents is not available.</p>`;
  if (!preview.length) return html`<p class="field-hint">No torrents in qBittorrent belong to this show.</p>`;
  return html`<ul class="torrent-list">${preview.map(t => html`<li key=${t.hash}>
    <span class="mono truncate" title=${t.name || t.hash}>${t.name || t.hash}</span>${t.tracked ? null : html`<span class="muted">found in its folder</span>`}
  </li>`)}</ul>`;
}

// Returns true when the show was deleted.
export async function deleteShow(show) {
  let preview = null;
  try { preview = await apiFetch(`/api/shows/${show.id}/delete-preview`, { quiet: true }); } catch (err) { /* the dialog says it is unavailable */ }
  const files = { checked: false };
  const confirmed = await confirmDialog({
    title: `Delete '${show.title}'?`,
    danger: true,
    confirm: 'Delete',
    body: html`<p class="field-hint">Removes the show and the torrents listed below, whether they are downloading or seeding.</p>
      ${torrentList(preview)}
      <label class="check-row"><input type="checkbox" onChange=${(e) => { files.checked = e.target.checked; }} />
        ${Array.isArray(preview) && preview.length ? `Also delete the downloaded files of ${preview.length === 1 ? 'this torrent' : `these ${preview.length} torrents`}` : 'Also delete the downloaded files'}</label>`,
  });
  if (!confirmed) return false;
  try {
    await apiFetch(`/api/shows/${show.id}?delete_torrents=true&delete_files=${files.checked}`, { method: 'DELETE' });
    toast(`Deleted ${show.title}.`, 'success');
    return true;
  } catch (err) { return false; }
}

function Actions({ show, small = false }) {
  const stop = (fn) => (e) => { e.stopPropagation(); fn(); };
  return html`<div class=${small ? 'row-actions' : 'card-actions'}>
    ${show.status !== 'completed' ? html`<button type="button" class="icon-btn" onClick=${stop(() => togglePause(show))} title=${show.paused ? 'Resume monitoring' : 'Pause monitoring'} aria-label=${show.paused ? 'Resume monitoring' : 'Pause monitoring'}><${Icon} name=${show.paused ? 'play' : 'pause'} small /></button>` : null}
    <button type="button" class="icon-btn danger" onClick=${stop(() => deleteShow(show))} title="Remove show" aria-label="Remove show"><${Icon} name="trash" small /></button>
  </div>`;
}

const feedName = (show) => (show.feed ? show.feed.name : 'No feed');

export function ShowCard({ show, now }) {
  const air = airSummary(show, now);
  const hasCount = show.progress.done > 0 || show.progress.total > 0;
  const hasAir = !!(air.ep || air.info);
  return html`<div class="show-card" role="link" tabindex="0" onPointerEnter=${() => prefetch(show.id)} onClick=${() => go(show.id)} onKeyDown=${(e) => { if (e.key === 'Enter') go(show.id); }}>
    <div class=${`poster-frame${dimmed(show) ? ' dim' : ''}`}>
      <${Poster} show=${show} zoom />
      <div class="tag-slot"><${StatusTag} show=${show} /></div>
      <${Actions} show=${show} />
      <${ProgressBar} show=${show} />
    </div>
    <div class="show-text">
      <h3 class="truncate" title=${show.title}>${show.title}</h3>
      ${hasAir || hasCount ? html`<div class="meta">
        ${hasAir ? html`<div class="air"><span class="ep">${air.ep}</span><span class=${`when ${air.tone}`} title=${air.at ? localDateTime(air.at) : undefined}>${air.info}</span></div>` : html`<${Problems} show=${show} />`}
        ${hasCount ? html`<span class="count" title=${`${countText(show)} downloaded`}>${countText(show)}</span>` : null}
      </div>` : null}
      ${hasAir ? html`<${Problems} show=${show} />` : hasCount ? null : html`<${Problems} show=${show} />`}
      <div class="meta feed-line" title=${feedName(show)}><${Icon} name="feeds" small /><span class="truncate">${feedName(show)}</span></div>
    </div>
  </div>`;
}

export function ShowRow({ show, now }) {
  const air = airSummary(show, now);
  return html`<div class="show-row" role="link" tabindex="0" onPointerEnter=${() => prefetch(show.id)} onClick=${() => go(show.id)} onKeyDown=${(e) => { if (e.key === 'Enter') go(show.id); }}>
    <div class="sr-poster"><${Poster} show=${show} /></div>
    <div class="sr-title">
      <h3 class="truncate" title=${show.title}>${show.title}</h3>
      <div class="sr-feed-sub truncate" title=${feedName(show)}>${feedName(show)}${show.counts.failed ? html`<span class="bad"> ${show.counts.failed} failed</span>` : null}${show.counts.missed ? html`<span class="warn"> ${show.counts.missed} missed</span>` : null}</div>
    </div>
    <div class="sr-air"><span class="ep">${air.ep}</span><span class=${`when ${air.tone}`} title=${air.at ? localDateTime(air.at) : undefined}>${air.info}</span></div>
    <div class="sr-feed" title=${feedName(show)}><${Icon} name="feeds" small /><span class="truncate">${feedName(show)}</span></div>
    <div class="sr-progress" title=${`${countText(show)} downloaded`}><${ProgressBar} show=${show} /><span class="count">${countText(show)}</span></div>
    <div class="sr-chip"><${StatusTag} show=${show} /></div>
    <${Actions} show=${show} small />
  </div>`;
}
