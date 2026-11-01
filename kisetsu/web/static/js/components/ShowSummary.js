// The pieces every place that shows a show is built from: poster, progress, air line, tags and problems.
import { html, useState } from '../ui.js';
import { apiFetch } from '../api.js';
import { paletteFromHues, posterEdgeColors, posterTextColor } from '../color.js';
import { countdown, initials, localDateTime, plural, progressFraction } from '../format.js';
import { go, prefetch } from '../router.js';
import { useTheme } from '../theme.js';
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
  // AniList's next airing moves on as soon as an episode airs, so the newest aired one that is not on disk yet is
  // still the one being looked for.
  const aired = (show.airings || []).filter(a => Date.parse(a.at) <= now);
  const newest = aired[aired.length - 1];
  if (newest && newest.status === 'downloading') return { ep: `Ep ${newest.episode}`, info: 'Downloading', tone: '', at: null };
  if (newest && (newest.status === 'wanted' || newest.status === 'missed')) {
    return { ep: `Ep ${newest.episode}`, info: 'Aired', tone: show.status !== 'waiting' ? 'warn' : '', at: newest.at };
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
export function Poster({ show, dim: dimmedOverride }) {
  const [broken, setBroken] = useState(false);
  const dim = (dimmedOverride === undefined ? dimmed(show) : dimmedOverride) ? ' dim' : '';
  const art = show.cover && !broken
    ? html`<img src=${show.cover} alt=${show.title} loading="lazy" class=${dim.trim()} onError=${() => setBroken(true)} />`
    : html`<div class=${`no-art${dim}`}>${initials(show.title)}</div>`;
  return html`<div class="poster on-art" data-poster-id=${show.id}>${art}</div>`;
}

// Filled with the accent the show's own page uses (its --ac-art), or the user's when its colours are not computed yet.
export function ProgressBar({ show }) {
  const { done, total } = show.progress;
  const { resolved } = useTheme();
  const known = total > 0 || done > 0 || !!show.next_airing;
  const pct = known ? Math.round(progressFraction(show.progress) * 100) : 0;
  const hues = show.accent_hues;
  const color = hues && hues.length >= 4 ? paletteFromHues(hues, resolved).ac : null;
  return html`<div class="bar" title=${`${done}/${total || '?'} downloaded`} style=${color ? `--bar:${color}` : undefined}><div class=${dimmed(show) ? 'dim' : ''} style=${`width:${pct}%`}></div></div>`;
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

function torrentList(torrents) {
  if (!Array.isArray(torrents)) return html`<p class="field-hint">The list of torrents is not available.</p>`;
  if (!torrents.length) return html`<p class="field-hint">No torrents in qBittorrent belong to this show.</p>`;
  return html`<div>
    <div class="field-label">Torrents <span class="count-badge">${torrents.length}</span></div>
    <ul class="torrent-list">${torrents.map(t => html`<li key=${t.hash}>
      <span class="mono truncate" title=${t.name || t.hash}>${t.name || t.hash}</span>${t.tracked ? null : html`<span class="muted">found in its folder</span>`}
    </li>`)}</ul>
  </div>`;
}

function folderNote(folder) {
  if (!folder || !folder.exists) return null;
  if (!folder.remove) return html`<p class="field-hint">The folder <span class="mono">${folder.path}</span> is kept: ${folder.reason} Only this show's files are deleted.</p>`;
  const more = folder.leftover_count - folder.leftovers.length;
  return html`<p class="field-hint">The folder <span class="mono">${folder.path}</span> is deleted too${folder.leftover_count ? ', with these files no torrent owns:' : '.'}</p>
    ${folder.leftover_count ? html`<ul class="torrent-list">${folder.leftovers.map(name => html`<li key=${name}><span class="mono truncate" title=${name}>${name}</span></li>`)}
      ${more > 0 ? html`<li><span class="muted">and ${more} more</span></li>` : null}</ul>` : null}`;
}

// A nameless torrent is a stale hash qBittorrent no longer lists, and a folder that stays loses nothing.
const hasFiles = ({ torrents, folder }) => torrents.some(t => t.name) || !!(folder && folder.remove && folder.leftover_count);

// The body re-renders on the switch; `keep` is what the caller reads once the dialog closes.
function DeleteBody({ preview, keep }) {
  const [kept, setKept] = useState(false);
  if (preview && !hasFiles(preview)) {
    return html`<p class="field-hint" style="margin:0">Removes the show. It has no torrents or files to delete.</p>
      ${preview.folder && preview.folder.remove ? folderNote(preview.folder) : null}`;
  }
  return html`<p class="field-hint" style="margin:0">${kept ? 'Removes the show and its torrents. The files stay on disk.' : 'Removes the show, its torrents and their files.'}</p>
    ${torrentList(preview ? preview.torrents : null)}
    ${kept ? null : folderNote(preview && preview.folder)}
    <label class="switch-row"><span>Keep files</span>
      <input type="checkbox" class="sr-only" checked=${kept} onChange=${(e) => { keep.checked = e.target.checked; setKept(e.target.checked); }} />
      <span class="switch" aria-hidden="true"></span></label>`;
}

// Returns true when the show was deleted.
export async function deleteShow(show) {
  let preview = null;
  try { preview = await apiFetch(`/api/shows/${show.id}/delete-preview`, { quiet: true }); } catch (err) { /* the dialog says it is unavailable */ }
  const keep = { checked: false };
  const confirmed = await confirmDialog({
    title: `Delete '${show.title}'?`,
    icon: 'trash',
    danger: true,
    confirm: 'Delete',
    body: html`<${DeleteBody} preview=${preview} keep=${keep} />`,
  });
  if (!confirmed) return false;
  try {
    const result = await apiFetch(`/api/shows/${show.id}?delete_torrents=true&delete_files=${!keep.checked}`, { method: 'DELETE' });
    const folder = result && result.folder;
    if (folder && !folder.removed) toast(`Deleted ${show.title}. Its folder was kept: ${folder.reason}`, 'info');
    else toast(`Deleted ${show.title}.`, 'success');
    return true;
  } catch (err) { return false; }
}

function RowActions({ show }) {
  const stop = (fn) => (e) => { e.stopPropagation(); fn(); };
  return html`<div class="row-actions">
    ${show.status !== 'completed' ? html`<button type="button" class="btn plain icon-only sm" onClick=${stop(() => togglePause(show))} title=${show.paused ? 'Resume monitoring' : 'Pause monitoring'} aria-label=${show.paused ? 'Resume monitoring' : 'Pause monitoring'}><${Icon} name=${show.paused ? 'play' : 'pause'} small /></button>` : null}
    <button type="button" class="btn plain icon-only sm" onClick=${stop(() => deleteShow(show))} title="Remove show" aria-label="Remove show"><${Icon} name="trash" small /></button>
  </div>`;
}

const feedName = (show) => (show.feed ? show.feed.name : 'No feed');

// The poster's own bottom edge colours what it sits on; AniList's cover colour stands in for the title until
// the edge is read, or when the edge is too grey to give it a hue.
export function posterVars({ cover_edge, cover_color }, mode) {
  const edge = posterEdgeColors(cover_edge, mode);
  const text = (edge && edge.text) || posterTextColor(cover_color, mode);
  return [text && `--show-text:${text}`, edge && `--show-panel:${edge.panel}`].filter(Boolean).join(';') || undefined;
}

export function ShowCard({ show, now }) {
  const air = airSummary(show, now);
  const hasCount = show.progress.done > 0 || show.progress.total > 0;
  const hasAir = !!(air.ep || air.info);
  const { resolved } = useTheme();
  return html`<div class="show-card" role="link" tabindex="0" style=${posterVars(show, resolved)} onPointerEnter=${() => prefetch(show.id)} onClick=${() => go(show.id)} onKeyDown=${(e) => { if (e.key === 'Enter') go(show.id); }}>
    <div class="poster-frame">
      <${Poster} show=${show} />
      ${show.status !== 'waiting' ? html`<div class="tag-slot"><${StatusTag} show=${show} /></div>` : null}
    </div>
    <div class="show-text">
      <h3 class="truncate" title=${show.title}>${show.title}</h3>
      ${hasAir || hasCount ? html`<div class="meta">
        ${hasAir ? html`<div class="air"><span class="ep">${air.ep}</span><span class=${`when ${air.tone}`} title=${air.at ? localDateTime(air.at) : undefined}>${air.info}</span></div>` : html`<${Problems} show=${show} />`}
        ${hasCount ? html`<span class="count" title=${`${countText(show)} downloaded`}>${countText(show)}</span>` : null}
      </div>` : null}
      ${hasAir ? html`<${Problems} show=${show} />` : hasCount ? null : html`<${Problems} show=${show} />`}
      ${show.feed && show.feed.failing ? html`<div class="meta warn truncate" title=${`${show.feed.name} is failing`}>${show.feed.name} is failing</div>` : null}
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
    <${RowActions} show=${show} />
  </div>`;
}
