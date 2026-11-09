import { html, useEffect, useMemo, useState } from '../ui.js';
import { apiFetch } from '../api.js';
import { useResource } from '../store.js';
import { settings as settingsData } from '../data.js';
import { applyUserAccent, currentAccent, setThemeMode, themeMode, useTheme } from '../theme.js';
import { exportBackup, restoreBackup } from '../components/Backup.js';
import { Seg, SaveBar, SetRow, Swatches } from '../components/Controls.js';
import { LoadError } from '../components/LoadError.js';
import { confirmDialog } from '../components/Modal.js';
import { toast } from '../components/Toast.js';
import { useDraft } from '../components/useDraft.js';

const NUMBER_FIELDS = ['default_seed_ratio', 'anilist_sync_minutes', 'feed_refresh_minutes', 'hunt_interval_minutes', 'early_air_tolerance_hours'];

function initialForm(s) {
  return {
    qbit_host: s.qbit_host, qbit_username: s.qbit_username, qbit_password: '', base_dir: s.base_dir, default_category: s.default_category,
    default_seed_ratio: s.default_seed_ratio, anilist_username: s.anilist_username, anilist_sync_minutes: s.anilist_sync_minutes,
    feed_refresh_minutes: s.feed_refresh_minutes, hunt_interval_minutes: s.hunt_interval_minutes, early_air_tolerance_hours: s.early_air_tolerance_hours,
  };
}

// A setting that applies the moment it is clicked; the previous value comes back if the server refuses it.
async function applyNow(patch, undo) {
  try { return await apiFetch('/api/settings', { method: 'POST', json: patch }); } catch (err) { if (undo) undo(); return null; }
}

export function Settings() {
  const { data, error, reload } = useResource(settingsData);
  const theme = useTheme();
  const initial = useMemo(() => (data ? initialForm(data) : {}), [data]);
  const form = useDraft(initial, 'settings');
  const [saving, setSaving] = useState(false);
  const [test, setTest] = useState({ state: '', text: '' });
  const [exporting, setExporting] = useState(false);
  const [restoring, setRestoring] = useState(false);
  if (!data) return html`<div class="page wide settings">${error ? html`<${LoadError} what="the settings" error=${error} reload=${reload} />` : null}</div>`;
  const bind = (key) => form.bind(key);

  const save = async () => {
    const changed = form.changed();
    const patch = {};
    Object.keys(changed).forEach(key => {
      const raw = changed[key];
      if (key === 'qbit_password') { if (raw) patch[key] = raw; return; }
      patch[key] = NUMBER_FIELDS.includes(key) ? Number(raw) : raw;
    });
    setSaving(true);
    const result = await applyNow(patch);
    setSaving(false);
    if (!result) return;
    toast(result.message || 'Settings saved.', 'success');
    (result.logs || []).forEach(line => toast(line, 'info'));
    form.reset();
    reload();
  };

  const testConnection = async () => {
    setTest({ state: 'wait', text: 'Testing…' });
    const body = { qbit_host: form.value('qbit_host'), qbit_username: form.value('qbit_username') };
    if (form.value('qbit_password')) body.qbit_password = form.value('qbit_password');
    try {
      const result = await apiFetch('/api/settings/test-qbit', { method: 'POST', json: body, quiet: true });
      setTest({ state: 'ok', text: `Connected (qBit ${result.app_version})` });
    } catch (err) { setTest({ state: 'bad', text: err.message }); }
  };

  const setNotifications = async (on) => { await applyNow({ desktop_notifications: on }); reload(); };
  const setMinResolution = async (value) => { if (await applyNow({ min_resolution: value })) reload(); };
  const setLanguage = async (lang) => {
    if (lang === data.title_language) return;
    let moveExisting = false;
    try {
      const plan = await apiFetch(`/api/settings/title-language-preview?lang=${encodeURIComponent(lang)}`);
      if (plan.torrents > 0) {
        const ok = await confirmDialog({
          title: `Switch to ${lang === 'romaji' ? 'Romaji' : 'English'} names?`,
          confirm: 'Switch and move',
          body: html`<p>This renames the download folders of ${plan.shows.length} show${plan.shows.length === 1 ? '' : 's'}. Their ${plan.torrents} download${plan.torrents === 1 ? '' : 's'} will move to the new folders.</p>
            <ul class="torrent-list">${plan.shows.map(s => html`<li key=${s.name}><span class="mono">${s.old} → ${s.new}</span></li>`)}</ul>`,
        });
        if (!ok) return;
        moveExisting = true;
      }
    } catch (err) { return; }
    const result = await applyNow({ title_language: lang, move_existing: moveExisting });
    if (result) { (result.logs || []).forEach(line => toast(line, 'info')); reload(); }
  };
  const setAccent = async (color, tint) => {
    const previous = currentAccent();
    applyUserAccent(color, tint);
    const result = await applyNow({ accent_color: color, accent_tint: tint }, () => applyUserAccent(previous.color, previous.tint));
    if (result) reload();
  };
  const clearAll = async () => {
    if (!(await confirmDialog({ title: 'Clear all shows?', body: html`<p>Removes every show and its history. Downloads in qBittorrent are kept.</p><p>Shows still on your AniList list come back with the next sync and look for their feed again.</p>`, confirm: 'Clear all', danger: true }))) return;
    try { const r = await apiFetch('/api/settings/clear-all', { method: 'POST' }); toast(r.message, 'success'); } catch (err) { /* shown by apiFetch */ }
  };

  const runExport = async () => {
    setExporting(true);
    try { await exportBackup(); } catch (err) { /* shown by apiDownload */ }
    setExporting(false);
  };

  const accent = currentAccent();
  return html`<div class="page wide settings">
    <div class="settings-cols">
      <div class="settings-col">
        <section class="sec-downloads">
          <h3 class="section-title section-gap">Downloads</h3>
          <div class="card card-divided form-grid">
            <${SetRow} name="Save to" htmlFor="set-base-dir" hint="{name} becomes the show name. Blank uses qBittorrent's default." size="wide"><input id="set-base-dir" class="field mono" placeholder="~/Anime/{name}" title="A folder as qBittorrent sees it (with Docker, the path inside the container)" ...${bind('base_dir')} /><//>
            <${SetRow} name="Category" htmlFor="set-category" size="mid"><input id="set-category" class="field" placeholder="(none)" ...${bind('default_category')} /><//>
            <${SetRow} name="Seed ratio" htmlFor="set-ratio" hint="Stop seeding at this ratio. Shows can set their own." size="num"><input id="set-ratio" type="number" step="0.1" min="0" class="field num" ...${bind('default_seed_ratio')} /><//>
            <${SetRow} name="Preferred quality" hint="Or the next one up when a feed lacks it. Never lower."><${Seg} label="Preferred quality" value=${data.min_resolution} onChange=${setMinResolution} options=${[[480, '480p'], [720, '720p'], [1080, '1080p']]} /><//>
            <${SetRow} name="Notifications" hint="A desktop popup when an episode finishes."><${Seg} label="Notifications" value=${data.desktop_notifications} onChange=${setNotifications} options=${[[true, 'On'], [false, 'Off']]} /><//>
          </div>
        </section>
        <section class="sec-backup">
          <h3 class="section-title section-gap">Backup</h3>
          <div class="card card-divided form-grid">
            <${SetRow} name="Export" hint="Settings, shows, feeds and history. Keep it private: feed addresses can hold keys."><button type="button" class="btn sm" onClick=${runExport} disabled=${exporting}>${exporting ? 'Exporting…' : 'Export backup'}</button><//>
            <${SetRow} name="Restore" hint="Replaces everything, after a preview. The current data is saved first."><button type="button" class="btn sm" onClick=${() => restoreBackup(setRestoring)} disabled=${restoring}>${restoring ? 'Working…' : 'Choose file…'}</button><//>
          </div>
        </section>
      </div>
      <div class="settings-col">
        <section class="sec-connections">
          <h3 class="section-title section-gap">Connections</h3>
          <div class="card card-divided form-grid">
            <${SetRow} name="qBittorrent" htmlFor="set-qbit-host" size="wide">
              <div class="set-stack">
                <input id="set-qbit-host" class="field" placeholder="http://localhost:8080" aria-label="qBittorrent host or URL" ...${bind('qbit_host')} />
                <div class="set-pair">
                  <input class="field" placeholder="Username" aria-label="qBittorrent username" ...${bind('qbit_username')} />
                  <input class="field" type="password" placeholder="Password" aria-label="qBittorrent password" autocomplete="new-password" ...${bind('qbit_password')} />
                </div>
                <p class="field-hint">Leave the password blank to keep the saved one.</p>
                <div class="row"><button type="button" class="btn sm" onClick=${testConnection}>Test connection</button><span class=${`test-${test.state}`}>${test.text}</span></div>
              </div>
            <//>
            <${SetRow} name="AniList" htmlFor="set-anilist" hint="Your username. Kisetsu follows what airs this season or next on your Watching and Planning lists." size="mid"><input id="set-anilist" class="field" ...${bind('anilist_username')} /><//>
          </div>
        </section>
        <section class="sec-appearance">
          <h3 class="section-title section-gap">Appearance</h3>
          <div class="card card-divided form-grid">
            <${SetRow} name="Theme"><${Seg} label="Theme" value=${themeMode()} onChange=${setThemeMode} options=${[['system', 'System'], ['light', 'Light'], ['dark', 'Dark']]} /><//>
            <${SetRow} name="Anime names"><${Seg} label="Anime names" value=${data.title_language} onChange=${setLanguage} options=${[['english', 'English'], ['romaji', 'Romaji']]} /><//>
            <${SetRow} name="Accent colour"><${Swatches} value=${accent.color} onPick=${(color) => setAccent(color.toLowerCase(), accent.tint)} onPreview=${(color) => applyUserAccent(color.toLowerCase(), accent.tint, { persist: false })} /><//>
            <${SetRow} name="Colour tint"><${Seg} label="Colour tint" value=${accent.tint} onChange=${(tint) => setAccent(accent.color, tint)} options=${[['off', 'Off'], ['subtle', 'Subtle'], ['full', 'Full']]} /><//>
          </div>
        </section>
      </div>
    </div>

    <details class="adv">
      <summary class="section-title">Advanced</summary>
      <div class="card card-divided form-grid">
        <${SetRow} name="AniList sync (min)" htmlFor="set-sync" hint="How often your AniList list is read." size="num"><input id="set-sync" type="number" min="1" max="10080" class="field num" ...${bind('anilist_sync_minutes')} /><//>
        <${SetRow} name="Feed check (min)" htmlFor="set-feeds" hint="How often a show's feed is read while an episode that aired in the last 3 days is still missing. Feeds no show needs are read every 6 hours." size="num"><input id="set-feeds" type="number" min="5" max="240" class="field num" ...${bind('feed_refresh_minutes')} /><//>
        <${SetRow} name="Hunt (min)" htmlFor="set-hunt" hint="Right after an episode airs, how often its show's feed is read until the release turns up." size="num"><input id="set-hunt" type="number" min="2" max="60" class="field num" ...${bind('hunt_interval_minutes')} /><//>
        <${SetRow} name="Early air (h)" htmlFor="set-early" hint="For a show's first episodes only: start looking this long before AniList's air time. 0 waits for the stated time." size="num"><input id="set-early" type="number" min="0" max="168" class="field num" ...${bind('early_air_tolerance_hours')} /><//>
      </div>
    </details>

    <div class="card form-grid danger-zone">
      <${SetRow} name="Clear all shows" hint="Removes every show and the history. Downloads are kept."><button type="button" class="btn danger sm" onClick=${clearAll}>Clear all</button><//>
    </div>

    <${SaveBar} open=${form.dirty} saving=${saving} onDiscard=${form.reset} onSave=${save} />
  </div>`;
}
