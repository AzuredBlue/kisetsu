// The feed list with drag-to-reorder, shared by the Feeds page and the setup wizard.
import { html, useState } from '../ui.js';
import { apiFetch } from '../api.js';
import { relativeTime } from '../format.js';
import { Icon } from './Icon.js';
import { confirmDialog, showModal } from './Modal.js';
import { toast } from './Toast.js';

export function feedHealth(feed) {
  if (!feed.failing_since) return feed.last_ok_at ? html`<span class="muted" title="The last time this feed was read without an error.">Read ${relativeTime(feed.last_ok_at)}</span>` : null;
  const since = new Date(feed.failing_since);
  const sameDay = since.toDateString() === new Date().toDateString();
  const when = sameDay ? since.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : since.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  return html`<span class="bad" title=${feed.last_error || `This feed has not been readable since ${since.toLocaleString()}`}>Failing since ${when}</span>`;
}

function FeedForm({ feed, close }) {
  const [name, setName] = useState(feed ? feed.name : '');
  const [url, setUrl] = useState(feed ? feed.url : '');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const submit = async (e) => {
    e.preventDefault();
    const body = {};
    if (feed) {
      if (name.trim() !== feed.name) body.name = name.trim();
      if (url.trim() !== feed.url) body.url = url.trim();
      if (!Object.keys(body).length) { close(false); return; }
    }
    setBusy(true);
    setError('');
    try {
      if (feed) await apiFetch(`/api/feeds/${feed.id}`, { method: 'PATCH', json: body, quiet: true });
      else await apiFetch('/api/feeds', { method: 'POST', json: { name: name.trim(), url: url.trim() }, quiet: true });
      toast(feed ? 'Feed updated.' : 'Feed added.', 'success');
      close(true);
    } catch (err) {
      setError(err.message);
      setBusy(false);
    }
  };
  return html`<form onSubmit=${submit}>
    <h2>${feed ? `Edit '${feed.name}'` : 'Add RSS feed'}</h2>
    <div class="body">
      <div><label class="field-label" for="feed-name">Name</label><input id="feed-name" class="field" required autocomplete="off" value=${name} onInput=${(e) => setName(e.target.value)} /></div>
      <div>
        <label class="field-label" for="feed-url">RSS URL</label>
        <input id="feed-url" class="field mono" type="url" required autocomplete="off" placeholder="https://…" value=${url} onInput=${(e) => setUrl(e.target.value)} />
        <p class="field-hint">${feed ? "Changing the URL keeps this feed's priority, locked shows and stored releases." : 'Added at the bottom of the list.'}</p>
      </div>
      ${error ? html`<p class="error-text" role="alert">${error}</p>` : null}
    </div>
    <div class="actions"><button type="button" class="btn" onClick=${() => close(false)}>Cancel</button><button type="submit" class="btn primary" disabled=${busy}>${feed ? 'Save' : 'Add'}</button></div>
  </form>`;
}

export const openFeedDialog = (feed = null) => showModal((close) => html`<${FeedForm} feed=${feed} close=${close} />`);

export async function deleteFeed(feed) {
  const usage = feed.shows > 0 ? ` ${feed.shows} show${feed.shows === 1 ? ' uses' : 's use'} it and will go back to Auto-Discover.` : '';
  const ok = await confirmDialog({
    title: `Delete '${feed.name}'?`,
    danger: true,
    confirm: 'Delete',
    body: html`<p>Removes the feed from Kisetsu. qBittorrent's own RSS list is not touched.${usage}</p>
      ${feed.locked > 0 ? html`<p class="error-text">${feed.locked} show${feed.locked === 1 ? ' is' : 's are'} locked to this feed; ${feed.locked === 1 ? 'its' : 'their'} lock is released.</p>` : null}`,
  });
  if (!ok) return;
  try {
    await apiFetch(`/api/feeds/${feed.id}`, { method: 'DELETE' });
    toast('Feed deleted.', 'success');
  } catch (err) { /* shown by apiFetch */ }
}

export async function importFromQbit() {
  try {
    const { added } = await apiFetch('/api/feeds/sync', { method: 'POST' });
    toast(added ? `Imported ${added} feed${added === 1 ? '' : 's'} from qBittorrent.` : 'Every feed in qBittorrent is already here.', 'success');
  } catch (err) { /* shown by apiFetch */ }
}

export function FeedsTable({ feeds, editable = true, selected = null, onSelect = null }) {
  const [drag, setDrag] = useState({ from: null, over: null });
  const move = async (from, to) => {
    if (to < 0 || to >= feeds.length || from === to) return;
    const ids = feeds.map(f => f.id);
    ids.splice(to, 0, ids.splice(from, 1)[0]);
    try { await apiFetch('/api/feeds/reorder', { method: 'POST', json: { ids } }); } catch (err) { /* shown by apiFetch */ }
  };
  if (!feeds.length) return html`<div class="empty">No feeds yet. Add one, or import the ones qBittorrent already has.</div>`;
  return html`<ul class="feed-list">
    ${feeds.map((feed, index) => html`<li key=${feed.id} draggable=${editable} class=${`feed-row${drag.over === index ? ' over' : ''}${drag.from === index ? ' dragging' : ''}${selected === feed.id ? ' selected' : ''}`}
      onDragStart=${(e) => { setDrag({ from: index, over: null }); e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', String(index)); }}
      onDragOver=${(e) => { e.preventDefault(); if (drag.over !== index) setDrag(d => ({ ...d, over: index })); }}
      onDragEnd=${() => setDrag({ from: null, over: null })}
      onDrop=${(e) => { e.preventDefault(); const from = drag.from; setDrag({ from: null, over: null }); if (from !== null) move(from, index); }}>
      ${editable ? html`<span class="grip" aria-hidden="true"><${Icon} name="grip" /></span>` : null}
      <span class="prio tabular">#${feed.priority}
        ${editable ? html`<span class="prio-move">
          <button type="button" disabled=${index === 0} onClick=${() => move(index, index - 1)} aria-label="Move up" title="Move up"><${Icon} name="up" small /></button>
          <button type="button" disabled=${index === feeds.length - 1} onClick=${() => move(index, index + 1)} aria-label="Move down" title="Move down"><${Icon} name="down" small /></button>
        </span>` : null}</span>
      <div class=${`feed-main${onSelect ? ' pickable' : ''}`} onClick=${onSelect ? () => onSelect(selected === feed.id ? null : feed.id) : undefined}>
        <div class="row"><span class="feed-name">${feed.name}</span>${feedHealth(feed)}</div>
        <div class="feed-url mono truncate" title=${feed.url}>${feed.url}</div>
      </div>
      <div class="feed-use tabular">${feed.shows > 0 ? html`<div>${feed.shows} show${feed.shows === 1 ? '' : 's'}</div>${feed.locked > 0 ? html`<div>${feed.locked} locked</div>` : null}` : null}</div>
      ${editable ? html`<div class="feed-actions">
        <button type="button" class="btn plain icon-only sm" draggable="false" onClick=${async () => { await openFeedDialog(feed); }} title="Edit feed" aria-label="Edit feed"><${Icon} name="edit" small /></button>
        <button type="button" class="btn plain icon-only sm" draggable="false" onClick=${() => deleteFeed(feed)} title="Delete feed" aria-label="Delete feed"><${Icon} name="trash" small /></button>
      </div>` : null}
    </li>`)}
  </ul>`;
}
