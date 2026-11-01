// Exporting everything to a file, and restoring from one: from the Settings page and from the first-run wizard.
import { html } from '../ui.js';
import { apiDownload, apiFetch } from '../api.js';
import { localTime, plural } from '../format.js';
import { setThemeMode, themeMode } from '../theme.js';
import { confirmDialog, showModal } from './Modal.js';

const PASSWORD_HEADER = 'X-Kisetsu-Qbit-Password';

export function exportBackup() {
  return apiDownload(`/api/backup/export?theme=${encodeURIComponent(themeMode())}`);
}

function pickFile() {
  return new Promise(resolve => {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = '.zip,application/zip';
    input.addEventListener('change', () => resolve(input.files[0] || null));
    input.addEventListener('cancel', () => resolve(null));
    input.click();
  });
}

function Compare({ backup, current }) {
  const rows = [
    ['Shows', backup.shows, current.shows],
    ['Feeds', backup.feeds, current.feeds],
    ['Episodes downloaded', backup.downloaded, current.downloaded],
    ['AniList user', backup.anilist_username || '—', current.anilist_username || '—'],
    ['qBittorrent', backup.qbit_host, current.qbit_host],
  ];
  return html`<table class="backup-compare">
    <thead><tr><td></td><th scope="col">In the backup</th><th scope="col">Here now</th></tr></thead>
    <tbody>${rows.map(([name, ours, here]) => html`<tr key=${name}><th scope="row">${name}</th><td>${ours}</td><td>${here}</td></tr>`)}</tbody>
  </table>`;
}

// Picks a file, shows what it holds, and replaces everything with it once confirmed.
export async function restoreBackup(setBusy) {
  const file = await pickFile();
  if (!file) return;
  setBusy(true);
  let plan;
  try {
    plan = await apiFetch('/api/backup/inspect', { method: 'POST', file });
  } catch (err) { setBusy(false); return; }
  setBusy(false);

  let password = '';
  // With the year, unlike the dates elsewhere: a backup can be old.
  const day = plan.exported_at ? new Date(plan.exported_at).toLocaleDateString([], { day: 'numeric', month: 'long', year: 'numeric' }) : '';
  const made = day ? `Made on ${day} at ${localTime(plan.exported_at)}.` : '';
  const ok = await confirmDialog({
    title: 'Restore this backup?',
    confirm: 'Replace and restore',
    danger: true,
    wide: true,
    body: html`<p>${made} This replaces everything Kisetsu has here. Torrents and files in qBittorrent are not touched.</p>
      <${Compare} backup=${plan.backup} current=${plan.current} />
      ${plan.password_needed ? html`<div>
        <label class="set-name" for="restore-password">qBittorrent password</label>
        <input id="restore-password" class="field" type="password" autocomplete="new-password" style="margin-top:.375rem" onInput=${(e) => { password = e.target.value; }} />
        <p class="field-hint">Backups don't include it. Leave it blank if qBittorrent doesn't ask for one.</p>
      </div>` : null}`,
  });
  if (!ok) return;

  setBusy(true);
  let result;
  try {
    const headers = password ? { [PASSWORD_HEADER]: encodeURIComponent(password) } : {};
    result = await apiFetch('/api/backup/restore', { method: 'POST', file, headers });
  } catch (err) { setBusy(false); return; }
  if (result.theme) setThemeMode(result.theme);
  await showModal((close) => html`
    <h2>Backup restored</h2>
    <div class="body">
      <p>Kisetsu now has ${plural(result.shows, 'show')} and is checking them against qBittorrent.</p>
      ${result.saved_as ? html`<p>What was here before is saved as a backup you can restore: <span class="mono">${result.saved_as}</span></p>` : null}
    </div>
    <div class="actions"><button class="btn primary" onClick=${() => close()}>Continue</button></div>`);
  location.reload();
}
