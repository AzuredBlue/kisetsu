import { html, useEffect, useMemo, useState } from '../ui.js';
import { apiFetch } from '../api.js';
import { useResource } from '../store.js';
import { settings as settingsData } from '../data.js';
import { applyUserAccent, currentAccent, setThemeMode, themeMode, useTheme } from '../theme.js';
import { Seg, SaveBar, SetRow, Swatches } from '../components/Controls.js';
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
  const { data, reload } = useResource(settingsData);
  const theme = useTheme();
  const initial = useMemo(() => (data ? initialForm(data) : {}), [data]);
  const form = useDraft(initial, 'settings');
  const [saving, setSaving] = useState(false);
  const [test, setTest] = useState({ state: '', text: '' });
  if (!data) return html`<div class="page medium"></div>`;
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
    if (!(await confirmDialog({ title: 'Clear all shows?', body: html`<p>Removes every show and its history. Downloads in qBittorrent are kept.</p>`, confirm: 'Clear all', danger: true }))) return;
    try { const r = await apiFetch('/api/settings/clear-all', { method: 'POST' }); toast(r.message, 'success'); } catch (err) { /* shown by apiFetch */ }
  };

  const accent = currentAccent();
  return html`<div class="page medium">
    <section>
      <h3 class="section-title section-gap">Downloading</h3>
      <div class="card card-divided">
        <${SetRow} name="Save to" htmlFor="set-base-dir" hint="{name} becomes the show name. Blank uses qBittorrent's default." size="wide"><input id="set-base-dir" class="field mono" placeholder="~/Anime/{name}" ...${bind('base_dir')} /><//>
        <${SetRow} name="Category" htmlFor="set-category" size="mid"><input id="set-category" class="field" placeholder="(blank)" ...${bind('default_category')} /><//>
        <${SetRow} name="Notifications" hint="A desktop popup when an episode finishes downloading."><${Seg} label="Notifications" value=${data.desktop_notifications} onChange=${setNotifications} options=${[[true, 'On'], [false, 'Off']]} /><//>
      </div>
    </section>

    <section>
      <h3 class="section-title section-gap">Connections</h3>
      <div class="card card-divided">
        <div class="set-row">
          <div class="set-text">
            <div class="set-name">qBittorrent</div>
            <p class="field-hint">Leave the password blank to keep the saved one.</p>
            <div class="row" style="margin-top:.5rem"><button type="button" class="btn sm" onClick=${testConnection}>Test connection</button><span class=${`mono test-${test.state}`}>${test.text}</span></div>
          </div>
          <div class="set-ctl wide set-stack">
            <input class="field" placeholder="http://localhost:8080" aria-label="qBittorrent host or URL" ...${bind('qbit_host')} />
            <div class="set-pair">
              <input class="field" placeholder="Username" aria-label="qBittorrent username" ...${bind('qbit_username')} />
              <input class="field" type="password" placeholder="Password" aria-label="qBittorrent password" autocomplete="new-password" ...${bind('qbit_password')} />
            </div>
          </div>
        </div>
        <${SetRow} name="AniList username" htmlFor="set-anilist" hint="Whose watching list to follow." size="mid"><input id="set-anilist" class="field" ...${bind('anilist_username')} /><//>
      </div>
    </section>

    <section>
      <h3 class="section-title section-gap">Appearance</h3>
      <div class="card card-divided">
        <${SetRow} name="Theme" hint="Saved in this browser only. System follows your OS setting."><${Seg} label="Theme" value=${themeMode()} onChange=${setThemeMode} options=${[['system', 'System'], ['light', 'Light'], ['dark', 'Dark']]} /><//>
        <${SetRow} name="Anime names"><${Seg} label="Anime names" value=${data.title_language} onChange=${setLanguage} options=${[['english', 'English'], ['romaji', 'Romaji']]} /><//>
        <${SetRow} name="Accent colour"><${Swatches} value=${accent.color} onPick=${(color) => setAccent(color.toLowerCase(), accent.tint)} onPreview=${(color) => applyUserAccent(color.toLowerCase(), accent.tint, { persist: false })} /><//>
        <${SetRow} name="Colour tint" hint="How much the accent colours the backgrounds."><${Seg} label="Colour tint" value=${accent.tint} onChange=${(tint) => setAccent(accent.color, tint)} options=${[['off', 'Off'], ['subtle', 'Subtle'], ['full', 'Full']]} /><//>
      </div>
    </section>

    <details class="adv">
      <summary class="section-title">Advanced</summary>
      <div class="card card-divided">
        <${SetRow} name="AniList sync every (min)" htmlFor="set-sync" hint="How often to sync your AniList list." size="num"><input id="set-sync" type="number" min="1" max="10080" class="field num" ...${bind('anilist_sync_minutes')} /><//>
        <${SetRow} name="Check feeds every (min)" htmlFor="set-feeds" hint="How often every feed is read for releases that are not being hunted: late ones, replacements and movies." size="num"><input id="set-feeds" type="number" min="5" max="240" class="field num" ...${bind('feed_refresh_minutes')} /><//>
        <${SetRow} name="Hunt every (min)" htmlFor="set-hunt" hint="After an episode airs, how often to read its show's feed until the release turns up." size="num"><input id="set-hunt" type="number" min="2" max="60" class="field num" ...${bind('hunt_interval_minutes')} /><//>
        <${SetRow} name="Early air (h)" htmlFor="set-early" hint="For a show's first episodes only: start looking this long before AniList's air time. 0 waits for the stated time." size="num"><input id="set-early" type="number" min="0" max="168" class="field num" ...${bind('early_air_tolerance_hours')} /><//>
        <${SetRow} name="Seed ratio" htmlFor="set-ratio" hint="Default for shows that don't set their own." size="num"><input id="set-ratio" type="number" step="0.1" min="0" class="field num" ...${bind('default_seed_ratio')} /><//>
      </div>
    </details>

    <div class="card danger-zone">
      <div><h3 class="section-title">Clear all shows</h3><p class="field-hint" style="margin-top:.125rem">Removes every show and the history. Downloads are kept.</p></div>
      <button type="button" class="btn danger sm" onClick=${clearAll}>Clear all</button>
    </div>

    <${SaveBar} open=${form.dirty} saving=${saving} onDiscard=${form.reset} onSave=${save} />
  </div>`;
}
