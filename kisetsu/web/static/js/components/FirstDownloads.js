// The first downloads after setup: what the first grab would start, show by show, until the user confirms or puts it off.
// The setup's last step and the Shows page (when the user went on while the search ran) both show it.
import { html, useEffect, useRef, useState } from '../ui.js';
import { apiFetch } from '../api.js';
import { episodeLabel } from '../format.js';
import { Poster } from './ShowSummary.js';
import { toast } from './Toast.js';

export const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

// The search for older episodes is still going: no count yet (the app is starting), or one that has not ended.
export const searchingOf = (progress) => !progress || !progress.done;

export const problemOf = (info) => info && (info.qbit_error ? `Can't reach qBittorrent: ${info.qbit_error}` : info.anilist_error ? `AniList: ${info.anilist_error}` : Object.values(info.jobs_failing || {})[0]);

// The shows with the episodes each would download, read again each time a search finishes so the list grows with it.
export function useFirstDownloads(progress, active = true) {
  const [shows, setShows] = useState(null);
  const [error, setError] = useState('');
  // Reads overlap while searches finish quickly; only the newest one's answer is kept.
  const latest = useRef(0);
  useEffect(() => {
    if (!active) return;
    const read = ++latest.current;
    apiFetch('/api/setup/review', { quiet: true }).then(
      (body) => { if (read === latest.current) { setShows(body.shows); setError(''); } },
      (err) => { if (read === latest.current) setError(err.message); },
    );
  }, [active, progress && progress.searched, progress && progress.done]);
  return { shows, error };
}

// Starts the downloads, or keeps every show paused.
export async function decideFirstDownloads(download) {
  await apiFetch('/api/setup/review', { method: 'POST', json: { download }, quiet: true });
  if (!download) toast('Every show is paused. Choose Resume all in the sidebar when you are ready.', 'info');
}

export const totalOf = (shows) => (shows || []).reduce((n, s) => n + s.episodes.length, 0);

function ShowLine({ show }) {
  return html`<li class="setup-show">
    <div class="sr-poster"><${Poster} show=${show} dim=${false} small /></div>
    <span class="setup-show-title">${show.title}</span>
    ${show.episodes.length ? html`<span class="setup-show-eps tabular">${show.episodes.length === 1 ? 'Episode' : 'Episodes'} ${episodeLabel(show.episodes)}</span>` : null}
  </li>`;
}

export function ShowList({ shows }) {
  return html`<ul class="setup-shows card">${shows.map(show => html`<${ShowLine} key=${show.id} show=${show} />`)}</ul>`;
}

// How many episodes, and while the search goes on, how far it got. The total is only a promise once the search has ended.
export function Summary({ shows, progress }) {
  const searching = searchingOf(progress);
  const taking = shows.filter(s => s.episodes.length);
  const total = totalOf(shows);
  const searched = progress ? progress.searched : 0;
  const of = progress ? progress.total : shows.length;
  return html`
    <p class="setup-total"><strong class="tabular">${plural(total, 'episode')}</strong> from ${plural(taking.length, 'show')} ${searching ? 'found so far.' : 'will be added to qBittorrent.'}</p>
    ${searching ? html`<div class="setup-search">
      <div class="setup-bar" role="progressbar" aria-label="Looking for older episodes" aria-valuemin="0" aria-valuemax=${of} aria-valuenow=${searched}>
        <div style=${`width:${of ? Math.round((searched / of) * 100) : 0}%`}></div>
      </div>
      <div class="setup-bar-meta"><span>Looking for older episodes</span><span class="tabular">${searched} / ${of}</span></div>
    </div>` : null}`;
}

// Shows page: the first downloads wait for the search to end, then for the user to confirm them.
export function PendingFirstDownloads({ info }) {
  const pending = !!(info && info.review_pending);
  const progress = info && info.setup_progress;
  const { shows, error } = useFirstDownloads(progress, pending);
  const [busy, setBusy] = useState(false);
  if (!pending || !shows) return null;
  const searching = searchingOf(progress);
  const total = totalOf(shows);
  const decide = async (download) => {
    setBusy(true);
    try { await decideFirstDownloads(download); } catch (err) { toast(err.message, 'error'); }
    setBusy(false);
  };
  const problem = problemOf(info);
  return html`<section class="card first-downloads" aria-label="First downloads">
    <h2 class="section-title">${searching ? 'Finding episodes' : total ? 'Ready to download' : 'Nothing to download yet'}</h2>
    ${!searching && !total
      ? html`<p class="setup-total">None of your shows has a released episode to download. New episodes download as they air.</p>`
      : html`<${Summary} shows=${shows} progress=${progress} />`}
    ${problem || error ? html`<p class="setup-error" role="alert">${problem || error}</p>` : null}
    ${shows.length ? html`<details class="adv"><summary>${plural(shows.length, 'show')}</summary><${ShowList} shows=${[...shows.filter(s => s.episodes.length), ...shows.filter(s => !s.episodes.length)]} /></details>` : null}
    <div class="row first-downloads-foot">
      <span class="field-hint">Your shows stay paused until you choose.</span>
      <button type="button" class="btn" onClick=${() => decide(false)} disabled=${busy}>Not yet</button>
      <button type="button" class="btn primary" onClick=${() => decide(true)} disabled=${busy || searching}>${searching ? 'Download' : total ? `Download ${plural(total, 'episode')}` : 'Finish'}</button>
    </div>
  </section>`;
}
