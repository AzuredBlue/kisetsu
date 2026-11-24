import { html, useEffect, useMemo, useState } from '../ui.js';
import { apiFetch, ApiError } from '../api.js';
import { useResource } from '../store.js';
import { feeds as feedsData, settings as settingsData, status as statusData } from '../data.js';
import { applyUserAccent, currentAccent, setThemeMode, themeMode, useTheme } from '../theme.js';
import { restoreBackup } from '../components/Backup.js';
import { Seg, SetRow, Swatches } from '../components/Controls.js';
import { FeedsTable, importFromQbit, openFeedDialog } from '../components/FeedsTable.js';
import { useDraft } from '../components/useDraft.js';
import { confirmDialog } from '../components/Modal.js';
import { Spinner } from '../components/Icon.js';
import { LoadError } from '../components/LoadError.js';
import { Poster } from '../components/ShowSummary.js';
import { toast } from '../components/Toast.js';
import { episodeLabel } from '../format.js';

const STEPS = ['welcome', 'qbit', 'feeds', 'downloads', 'appearance', 'anilist', 'import', 'review'];
// Once the AniList step is finished there is no going back: the shows are in and wait to be confirmed.
const AFTER_FINISH = STEPS.indexOf('import');

const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

async function save(patch) {
  await apiFetch('/api/setup/save', { method: 'POST', json: patch });
}

const problemOf = (info) => info && (info.qbit_error ? `Can't reach qBittorrent: ${info.qbit_error}` : info.anilist_error ? `AniList: ${info.anilist_error}` : Object.values(info.jobs_failing || {})[0]);

// Until the shows are in and the feeds and qBittorrent are read; the search for older episodes goes on in the review.
function Importing({ info, onDone }) {
  const ready = !!(info && info.ready);
  useEffect(() => { if (ready) onDone(); }, [ready]);
  const phase = info && info.shows ? 'Checking the feeds and qBittorrent' : 'Reading your AniList list';
  const problem = problemOf(info);
  return html`
    <h2>Importing your shows</h2>
    <div class="setup-bar busy" role="progressbar" aria-label=${phase}><div></div></div>
    <div class="setup-bar-meta"><span>${phase}</span></div>
    ${problem ? html`<p class="setup-error" role="alert">${problem}</p>` : null}`;
}

// The search for older episodes is still going: no count yet, or one that has not ended.
const searchingOf = (progress) => !progress || !progress.done;

// What the first downloads will be, show by show, filling in while older episodes are searched for; the shows with nothing
// to take yet fold away. The total is only a promise once the search has ended.
function Review({ shows, progress, problem }) {
  if (!shows) return html`<h2>Finding episodes</h2><${Spinner} />`;
  const searching = searchingOf(progress);
  const taking = shows.filter(s => s.episodes.length);
  const idle = shows.filter(s => !s.episodes.length);
  const total = taking.reduce((n, s) => n + s.episodes.length, 0);
  const row = (show) => html`<li key=${show.id} class="setup-show">
    <div class="sr-poster"><${Poster} show=${show} dim=${false} small /></div>
    <span class="setup-show-title">${show.title}</span>
    ${show.episodes.length ? html`<span class="setup-show-eps tabular">${show.episodes.length === 1 ? 'Episode' : 'Episodes'} ${episodeLabel(show.episodes)}</span>` : null}
  </li>`;
  if (!total && !searching) {
    return html`
      <h2>Nothing to download yet</h2>
      <p class="page-sub">${shows.length ? `None of your ${plural(shows.length, 'show')} has a released episode to download. New episodes download as they air.` : 'No shows were imported. Add them later from Shows, Add show.'}</p>
      ${shows.length ? html`<ul class="setup-shows card">${shows.map(row)}</ul>` : null}`;
  }
  const searched = progress ? progress.searched : 0;
  const of = progress ? progress.total : shows.length;
  return html`
    <h2>${searching ? 'Finding episodes' : 'Ready to download'}</h2>
    <p class="setup-total">${searching
      ? html`<strong class="tabular">${plural(total, 'episode')}</strong> from ${plural(taking.length, 'show')} found so far.`
      : html`<strong class="tabular">${plural(total, 'episode')}</strong> from ${plural(taking.length, 'show')} will be added to qBittorrent.`}</p>
    ${searching ? html`<div class="setup-search">
      <div class="setup-bar" role="progressbar" aria-label="Looking for older episodes" aria-valuemin="0" aria-valuemax=${of} aria-valuenow=${searched}>
        <div style=${`width:${of ? Math.round((searched / of) * 100) : 0}%`}></div>
      </div>
      <div class="setup-bar-meta"><span>Looking for older episodes</span><span class="tabular">${searched} / ${of}</span></div>
    </div>` : null}
    ${problem ? html`<p class="setup-error" role="alert">${problem}</p>` : null}
    ${taking.length ? html`<ul class="setup-shows card">${taking.map(row)}</ul>` : null}
    ${idle.length ? html`<details class="adv setup-idle">
      <summary>${plural(idle.length, 'show')} with nothing to download yet</summary>
      <ul class="setup-shows card">${idle.map(row)}</ul>
    </details>` : null}
    <p class="field-hint">Not yet keeps every show paused until you choose Resume all in the sidebar.</p>`;
}

export function Setup({ sandbox, reviewing }) {
  useTheme();
  const settings = useResource(settingsData);
  const feeds = useResource(feedsData);
  const info = useResource(statusData).data;
  const [step, setStep] = useState(reviewing ? AFTER_FINISH : 0);
  const [review, setReview] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [tested, setTested] = useState({ state: '', text: '' });
  const [suggested, setSuggested] = useState(false);
  const [checked, setChecked] = useState({ state: '', text: '' });
  const data = settings.data;
  const initial = useMemo(() => (data ? {
    qbit_host: data.qbit_host, qbit_username: data.qbit_username, qbit_password: '', base_dir: data.base_dir,
    default_category: data.default_category, default_seed_ratio: data.default_seed_ratio, anilist_username: data.anilist_username,
    delete_watched: data.delete_watched,
  } : {}), [data]);
  const form = useDraft(initial, 'setup');
  const name = STEPS[step];
  useEffect(() => { setError(''); }, [step]);
  const progress = info && info.setup_progress;
  // Read again each time a search finishes, so the list grows with what it found.
  useEffect(() => {
    if (name !== 'review') return;
    apiFetch('/api/setup/review', { quiet: true }).then(setReview, (err) => setError(err.message));
  }, [name, progress && progress.searched, progress && progress.done]);
  if (!data) return html`<div class="setup">${settings.error ? html`<${LoadError} what="the settings" error=${settings.error} reload=${settings.reload} />` : html`<${Spinner} />`}</div>`;

  const connection = () => {
    const body = { qbit_host: form.value('qbit_host'), qbit_username: form.value('qbit_username') };
    if (form.value('qbit_password')) body.qbit_password = form.value('qbit_password');
    return body;
  };
  const testQbit = async () => {
    setTested({ state: 'wait', text: 'Testing…' });
    try {
      const r = await apiFetch('/api/settings/test-qbit', { method: 'POST', json: connection(), quiet: true });
      setTested({ state: 'ok', text: `Connected (qBit ${r.app_version})` });
      // The first answer fills Save to with a folder inside qBittorrent's own, unless one is typed already.
      if (!suggested && r.default_save_path && !form.value('base_dir').trim()) {
        const root = r.default_save_path.replace(/[\\/]+$/, '');
        const sep = root.includes('\\') && !root.includes('/') ? '\\' : '/';
        form.set('base_dir', `${root}${sep}Anime${sep}{name}`);
      }
      setSuggested(true);
    } catch (err) { setTested({ state: 'bad', text: err.message }); }
  };
  const checkAnilist = async () => {
    setChecked({ state: 'wait', text: 'Checking…' });
    try {
      const { shows } = await apiFetch('/api/setup/check-anilist', { method: 'POST', json: { anilist_username: form.value('anilist_username') }, quiet: true });
      setChecked({ state: 'ok', text: shows ? `Found ${shows} show${shows === 1 ? '' : 's'} airing this season or next.` : 'Found no shows airing this season or next. You can add them later.' });
    } catch (err) { setChecked({ state: 'bad', text: err.message }); }
  };
  const next = async () => {
    if (name === 'feeds' && feeds.data && !feeds.data.length) {
      const ok = await confirmDialog({
        title: 'Continue without RSS feeds?',
        body: html`<p>Nothing can be found or downloaded until you add a feed. You can add one later on the RSS Feeds page.</p>`,
        confirm: 'Continue',
      });
      if (!ok) return;
    }
    setBusy(true);
    setError('');
    try {
      if (name === 'qbit') await save(connection());
      if (name === 'downloads') await save({ base_dir: form.value('base_dir'), default_category: form.value('default_category'), default_seed_ratio: Number(form.value('default_seed_ratio')), delete_watched: form.value('delete_watched') });
      if (name === 'anilist') {
        await apiFetch('/api/setup/complete', { method: 'POST', json: { anilist_username: form.value('anilist_username') }, quiet: true });
        settings.reload();
      }
      setStep(step + 1);
    } catch (err) { setError(err instanceof ApiError || err instanceof Error ? err.message : String(err)); }
    setBusy(false);
  };
  const decide = async (download) => {
    setBusy(true);
    setError('');
    try {
      await apiFetch('/api/setup/review', { method: 'POST', json: { download }, quiet: true });
      if (!download) toast('Every show is paused. Choose Resume all in the sidebar when you are ready.', 'info');
    } catch (err) { setError(err.message); setBusy(false); }
  };
  const blocked = (name === 'qbit' && tested.state !== 'ok') || (name === 'anilist' && checked.state !== 'ok');
  const total = review ? review.shows.reduce((n, s) => n + s.episodes.length, 0) : 0;
  const accent = currentAccent();
  const applyAppearance = async (patch, undo) => {
    try { await save(patch); settings.reload(); } catch (err) { if (undo) undo(); }
  };

  return html`<div class="setup">
    <div class="setup-card" key=${name}>
      ${name === 'welcome' ? html`
        <h2>Welcome to Kisetsu</h2>
        <p class="page-sub">Kisetsu follows your AniList watching list and has qBittorrent download each new episode as it airs. A few questions first.</p>
        ${sandbox ? html`<p class="field-hint">Sandbox: downloads are pretended and logged. qBittorrent and your files are never changed, and nothing is kept once it stops.</p>` : null}` : null}
      ${name === 'qbit' ? html`
        <h2>qBittorrent</h2>
        <p class="page-sub">Turn on the Web UI in qBittorrent (Options, Web UI), then enter its address and login.</p>
        <div class="stack">
          <input class="field" placeholder="http://localhost:8080" aria-label="qBittorrent host or URL" ...${form.bind('qbit_host')} onInput=${(e) => { form.set('qbit_host', e.target.value); setTested({ state: '', text: '' }); }} />
          <div class="set-pair">
            <input class="field" placeholder="Username" aria-label="qBittorrent username" ...${form.bind('qbit_username')} onInput=${(e) => { form.set('qbit_username', e.target.value); setTested({ state: '', text: '' }); }} />
            <input class="field" type="password" placeholder="Password" aria-label="qBittorrent password" autocomplete="new-password" ...${form.bind('qbit_password')} onInput=${(e) => { form.set('qbit_password', e.target.value); setTested({ state: '', text: '' }); }} />
          </div>
          <div class="row"><button type="button" class="btn sm" onClick=${testQbit}>Test connection</button><span class=${`mono test-${tested.state}`}>${tested.text}</span></div>
        </div>` : null}
      ${name === 'feeds' ? html`
        <h2>RSS feeds</h2>
        <p class="page-sub">Where releases are found. When one is in several feeds, the higher one wins. You can change this later.</p>
        <div class="row"><button type="button" class="btn sm" onClick=${() => openFeedDialog()}>Add feed</button><button type="button" class="btn sm" onClick=${importFromQbit}>Import from qBittorrent</button></div>
        <div class="card" style="margin-top:.75rem">${feeds.data ? html`<${FeedsTable} feeds=${feeds.data} />` : feeds.error ? html`<${LoadError} what="the feeds" error=${feeds.error} reload=${feeds.reload} />` : null}</div>` : null}
      ${name === 'downloads' ? html`
        <h2>Downloads</h2>
        <div class="card card-divided">
          <${SetRow} name="Save to" htmlFor="su-base" hint="{name} becomes the show name. It must be a folder qBittorrent can see; with qBittorrent in Docker, the path inside the container. Blank uses qBittorrent's default." size="wide"><input id="su-base" class="field mono" placeholder="~/Anime/{name}" ...${form.bind('base_dir')} /><//>
          <${SetRow} name="Category" htmlFor="su-cat" size="mid"><input id="su-cat" class="field" placeholder="(blank)" ...${form.bind('default_category')} /><//>
          <${SetRow} name="Seed ratio" htmlFor="su-ratio" hint="Stop seeding at this ratio." size="num"><input id="su-ratio" type="number" step="0.1" min="0" class="field num" ...${form.bind('default_seed_ratio')} /><//>
          <${SetRow} name="Delete watched" hint="Skip episodes your AniList progress has passed, and delete each download with its files after you watch it, once it stops seeding."><${Seg} label="Delete watched" value=${form.value('delete_watched')} onChange=${(v) => form.set('delete_watched', v)} options=${[[true, 'On'], [false, 'Off']]} /><//>
        </div>` : null}
      ${name === 'appearance' ? html`
        <h2>Appearance</h2>
        <div class="card card-divided">
          <${SetRow} name="Theme"><${Seg} label="Theme" value=${themeMode()} onChange=${setThemeMode} options=${[['system', 'System'], ['light', 'Light'], ['dark', 'Dark']]} /><//>
          <${SetRow} name="Anime names"><${Seg} label="Anime names" value=${data.title_language} onChange=${(v) => applyAppearance({ title_language: v })} options=${[['english', 'English'], ['romaji', 'Romaji']]} /><//>
          <${SetRow} name="Colour"><${Swatches} value=${accent.color} onPick=${(c) => { const prev = accent; applyUserAccent(c.toLowerCase(), accent.tint); applyAppearance({ accent_color: c.toLowerCase() }, () => applyUserAccent(prev.color, prev.tint)); }} onPreview=${(c) => applyUserAccent(c.toLowerCase(), accent.tint, { persist: false })} /><//>
          <${SetRow} name="Colour tint" hint="How much the accent colours the backgrounds."><${Seg} label="Colour tint" value=${accent.tint} onChange=${(t) => { const prev = accent; applyUserAccent(accent.color, t); applyAppearance({ accent_tint: t }, () => applyUserAccent(prev.color, prev.tint)); }} options=${[['off', 'Off'], ['subtle', 'Subtle'], ['full', 'Full']]} /><//>
          <${SetRow} name="Notifications" hint="A desktop popup when an episode finishes downloading."><${Seg} label="Notifications" value=${data.desktop_notifications} onChange=${(v) => applyAppearance({ desktop_notifications: v })} options=${[[true, 'On'], [false, 'Off']]} /><//>
        </div>` : null}
      ${name === 'anilist' ? html`
        <h2>AniList</h2>
        <p class="page-sub">Your username. Kisetsu follows what airs this season or next on your Watching and Planning lists; anything else can be added later from Shows, Add show.</p>
        <div class="stack">
          <input class="field" placeholder="AniList username" aria-label="AniList username" autofocus ...${form.bind('anilist_username')} onInput=${(e) => { form.set('anilist_username', e.target.value); setChecked({ state: '', text: '' }); }} onKeyDown=${(e) => { if (e.key !== 'Enter' || busy || checked.state === 'wait' || !form.value('anilist_username').trim()) return; if (checked.state === 'ok') next(); else checkAnilist(); }} />
          <div class="row"><button type="button" class="btn sm" onClick=${checkAnilist} disabled=${!form.value('anilist_username').trim() || checked.state === 'wait'}>Check</button><span class=${`test-${checked.state}`} role="status">${checked.text}</span></div>
        </div>` : null}
      ${name === 'import' ? html`<${Importing} info=${info} onDone=${() => setStep(STEPS.indexOf('review'))} />` : null}
      ${name === 'review' ? html`<${Review} shows=${review && review.shows} progress=${progress} problem=${problemOf(info)} />` : null}
      ${error ? html`<p class="setup-error" role="alert">${error}</p>` : null}
      <div class="setup-foot">
        <div class="setup-dots" aria-label=${`Step ${step + 1} of ${STEPS.length}`}>${STEPS.map((s, i) => html`<span key=${s} class=${i === step ? 'on' : ''}></span>`)}</div>
        <div class="row">
          ${name === 'welcome' && !sandbox ? html`<button type="button" class="btn" onClick=${() => restoreBackup(setBusy)} disabled=${busy}>Restore a backup</button>` : null}
          ${step > 0 && step < AFTER_FINISH ? html`<button type="button" class="btn" onClick=${() => setStep(step - 1)} disabled=${busy}>Back</button>` : null}
          ${name === 'review' && (total || searchingOf(progress)) ? html`<button type="button" class="btn" onClick=${() => decide(false)} disabled=${busy}>Not yet</button>` : null}
          ${name === 'review'
            ? html`<button type="button" class="btn primary" onClick=${() => decide(true)} disabled=${busy || !review || searchingOf(progress)}>${busy ? 'Working…' : searchingOf(progress) ? 'Download' : total ? `Download ${plural(total, 'episode')}` : 'Finish'}</button>`
            : name === 'import' ? null
            : html`<button type="button" class="btn primary" onClick=${next} disabled=${busy || blocked}>${busy ? 'Working…' : name === 'welcome' ? 'Get started' : name === 'anilist' ? 'Import shows' : 'Next'}</button>`}
        </div>
      </div>
    </div>
  </div>`;
}
