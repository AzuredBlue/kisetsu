import { html, useEffect, useMemo, useState } from '../ui.js';
import { apiFetch, ApiError } from '../api.js';
import { useResource } from '../store.js';
import { feeds as feedsData, settings as settingsData } from '../data.js';
import { applyUserAccent, currentAccent, setThemeMode, themeMode, useTheme } from '../theme.js';
import { restoreBackup } from '../components/Backup.js';
import { Seg, SetRow, Swatches } from '../components/Controls.js';
import { FeedsTable, importFromQbit, openFeedDialog } from '../components/FeedsTable.js';
import { useDraft } from '../components/useDraft.js';
import { confirmDialog } from '../components/Modal.js';
import { Spinner } from '../components/Icon.js';
import { LoadError } from '../components/LoadError.js';

const STEPS = ['welcome', 'qbit', 'feeds', 'downloads', 'appearance', 'anilist'];

async function save(patch) {
  await apiFetch('/api/setup/save', { method: 'POST', json: patch });
}

export function Setup({ sandbox }) {
  useTheme();
  const settings = useResource(settingsData);
  const feeds = useResource(feedsData);
  const [step, setStep] = useState(0);
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
        setBusy(false);
        return;
      }
      setStep(step + 1);
    } catch (err) { setError(err instanceof ApiError || err instanceof Error ? err.message : String(err)); }
    setBusy(false);
  };
  const blocked = (name === 'qbit' && tested.state !== 'ok') || (name === 'anilist' && checked.state !== 'ok');
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
          <${SetRow} name="Accent colour"><${Swatches} value=${accent.color} onPick=${(c) => { const prev = accent; applyUserAccent(c.toLowerCase(), accent.tint); applyAppearance({ accent_color: c.toLowerCase() }, () => applyUserAccent(prev.color, prev.tint)); }} onPreview=${(c) => applyUserAccent(c.toLowerCase(), accent.tint, { persist: false })} /><//>
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
      ${error ? html`<p class="setup-error" role="alert">${error}</p>` : null}
      <div class="setup-foot">
        <div class="setup-dots" aria-label=${`Step ${step + 1} of ${STEPS.length}`}>${STEPS.map((s, i) => html`<span key=${s} class=${i === step ? 'on' : ''}></span>`)}</div>
        <div class="row">
          ${name === 'welcome' && !sandbox ? html`<button type="button" class="btn" onClick=${() => restoreBackup(setBusy)} disabled=${busy}>Restore a backup</button>` : null}
          ${step > 0 ? html`<button type="button" class="btn" onClick=${() => setStep(step - 1)} disabled=${busy}>Back</button>` : null}
          <button type="button" class="btn primary" onClick=${next} disabled=${busy || blocked}>${busy ? 'Working…' : name === 'welcome' ? 'Get started' : name === 'anilist' ? 'Finish' : 'Next'}</button>
        </div>
      </div>
    </div>
  </div>`;
}
