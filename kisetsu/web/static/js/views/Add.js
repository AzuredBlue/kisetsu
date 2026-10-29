// Adding a show by hand: pick an entry of the user's AniList list, then the feed and the episodes to download.
import { html, useEffect, useMemo, useState } from '../ui.js';
import { apiFetch } from '../api.js';
import { go } from '../router.js';
import { Icon, Spinner } from '../components/Icon.js';
import { Poster } from '../components/ShowSummary.js';
import { toast } from '../components/Toast.js';
import { relativeTime } from '../format.js';

const version = (v) => (v > 1 ? ` v${v}` : '');
const fold = (text) => String(text || '').normalize('NFKD').replace(/[̀-ͯ]/g, '').toLowerCase();
const AIR_LABELS = { RELEASING: 'Releasing', FINISHED: 'Finished', NOT_YET_RELEASED: 'Not yet released', HIATUS: 'On hiatus', CANCELLED: 'Cancelled' };

function EntryRow({ entry, onPick }) {
  const facts = [entry.list_label, AIR_LABELS[entry.status] || null, entry.season, entry.total_episodes ? `${entry.total_episodes} ep` : null].filter(Boolean).join(' · ');
  const art = { id: `al-${entry.anilist_id}`, title: entry.title, cover: entry.cover };
  return html`<li class="list-row add-entry">
    <div class="sr-poster"><${Poster} show=${art} dim=${false} /></div>
    <div class="grow">
      <div class="add-entry-title truncate" title=${entry.title}>${entry.title}</div>
      <div class="muted truncate">${facts}</div>
    </div>
    <span class="row-end">
      ${entry.followed
        ? html`<span class="muted">Already followed</span>`
        : html`<button type="button" class="btn sm" onClick=${() => onPick(entry)}>Select</button>`}
    </span>
  </li>`;
}

function PickEntry({ onPick }) {
  const [list, setList] = useState(null);
  const [error, setError] = useState(null);
  const [text, setText] = useState('');
  const [refreshing, setRefreshing] = useState(false);
  const load = async (refresh = false) => {
    setRefreshing(refresh);
    try {
      const body = await apiFetch(`/api/add/list${refresh ? '?refresh=true' : ''}`, { quiet: true });
      setList(body);
      setError(null);
      if (body.notice) toast(body.notice, 'info');
    } catch (err) { setError(err.message); }
    setRefreshing(false);
  };
  useEffect(() => { load(); }, []);
  const shown = useMemo(() => {
    if (!list) return [];
    const words = fold(text).split(/\s+/).filter(Boolean);
    if (!words.length) return list.entries;
    return list.entries.filter(e => {
      const hay = fold([e.title, ...(e.aliases || [])].join(' '));
      return words.every(w => hay.includes(w));
    });
  }, [list, text]);
  const wait = list ? Math.ceil(list.refresh_in || 0) : 0;
  return html`<div class="card list-card stack-sm">
    <div class="add-search">
      <${Icon} name="search" small />
      <input class="field" type="search" placeholder="Search your AniList list" value=${text} onInput=${(e) => setText(e.target.value)} autofocus aria-label="Search your AniList list" />
      <button type="button" class="btn sm" onClick=${() => load(true)} disabled=${refreshing || wait > 0} title=${wait > 0 ? `The list was read from AniList ${list && list.fetched_at ? relativeTime(list.fetched_at, Date.now()) : 'just now'}. It can be read again in ${wait}s.` : 'Read your list from AniList again'}>
        ${refreshing ? html`<${Spinner} small />` : html`<${Icon} name="refresh" small />`}<span>Refresh list</span>
      </button>
    </div>
    <p class="field-hint">Only your own AniList list is searched: entries you are watching, rewatching, have completed, paused or dropped. To add something else, put it on your list on AniList first.</p>
    ${error ? html`<p class="error-text">${error}</p>` : null}
    <ul class="list">
      ${!list && !error ? html`<li class="list-empty"><${Spinner} small /> Reading your list…</li>` : null}
      ${list && !shown.length ? html`<li class="list-empty">${text ? 'Nothing on your list matches.' : 'Your list has no entries to add.'}</li>` : null}
      ${shown.map(entry => html`<${EntryRow} key=${entry.anilist_id} entry=${entry} onPick=${onPick} />`)}
    </ul>
  </div>`;
}

function Notes({ result }) {
  const notes = [];
  if (result.folder && result.preselect) notes.push(['info', `Found ${result.folder.files} file(s) in ${result.folder.path}. The feed they came from is selected; the show will use that folder, and what is already there is checked rather than downloaded again.`]);
  if (result.note) notes.push(['warn', result.note]);
  if (result.unsearchable && result.unsearchable.length) notes.push(['info', `Only the RSS of ${result.unsearchable.join(', ')} was read: ${result.unsearchable.length === 1 ? 'its site' : 'their sites'} cannot be searched.`]);
  if (result.stopped) notes.push(['warn', `The search stopped early: ${result.stopped}`]);
  if (!notes.length) return null;
  return html`<div class="stack-sm">${notes.map(([tone, text], i) => html`<p key=${i} class=${`add-note ${tone}`}><${Icon} name=${tone === 'warn' ? 'alert' : 'info'} small /><span>${text}</span></p>`)}</div>`;
}

// The episodes the feed offers, and those the ticked releases cover; a ticked batch covers its singles.
function coverage(group, picked) {
  const offered = new Set([...group.episodes.map(e => e.number), ...(group.batches || []).flatMap(b => b.numbers)]);
  const inBatch = new Set((group.batches || []).filter(b => picked.has(b.item_id)).flatMap(b => b.numbers));
  const covered = new Set([...inBatch, ...group.episodes.filter(e => picked.has(e.item_id)).map(e => e.number)]);
  return { offered, inBatch, covered };
}

// Picking a feed takes its batches when it has any (the largest first, then those that add episodes), else every single episode.
// A batch that names its episodes is trusted over one that only says "Batch".
function defaultPick(group) {
  const batches = group.batches || [];
  if (!batches.length) return new Set(group.episodes.map(e => e.item_id));
  const ranged = batches.filter(b => b.ranged);
  const taken = new Set();
  const ids = new Set();
  (ranged.length ? ranged : batches).forEach(b => {
    if (b.numbers.some(n => taken.has(n))) return;
    ids.add(b.item_id);
    b.numbers.forEach(n => taken.add(n));
  });
  return ids;
}

const span = (numbers) => (numbers.length > 1 ? `Ep ${numbers[0]}–${numbers[numbers.length - 1]}` : `Ep ${numbers[0]}`);

function FeedGroup({ group, active, picked, onFeed, onToggle }) {
  const { offered, inBatch, covered } = coverage(group, active ? picked : new Set());
  const all = offered.size;
  return html`<li class="list-head add-feed-head" key=${`h:${group.feed_id}`}>
      <label class="add-check"><input type="radio" name="add-feed" checked=${active} onChange=${() => onFeed(group)} /><span class="truncate">${group.feed_name}</span></label>
      <span class="push muted tabular">${active ? `${covered.size} of ${all}` : `${all} episode${all === 1 ? '' : 's'}`}</span>
    </li>
    ${(group.batches || []).map(batch => html`<li class="list-row" key=${`b:${group.feed_id}:${batch.item_id}`}>
      <label class="add-check grow">
        <input type="checkbox" checked=${active && picked.has(batch.item_id)} onChange=${() => onToggle(group, batch)} />
        <span class="ep-no">${span(batch.numbers)}</span>
        <span class="release-name mono truncate" title=${batch.title}>${batch.title}</span>
      </label>
      <span class="row-end">${batch.on_disk ? html`<span class="state ok">${batch.on_disk} on disk</span>` : null}<span class="pill pill-info">Batch</span></span>
    </li>`)}
    ${group.episodes.map(ep => {
      const held = active && inBatch.has(ep.number);
      return html`<li class="list-row" key=${`e:${group.feed_id}:${ep.item_id}`}>
      <label class=${`add-check grow${held ? ' held' : ''}`} title=${held ? 'Comes with the ticked batch' : undefined}>
        <input type="checkbox" checked=${active && picked.has(ep.item_id) && !held} disabled=${held} onChange=${() => onToggle(group, ep)} />
        <span class="ep-no">Ep ${ep.number}${version(ep.version)}</span>
        <span class="release-name mono truncate" title=${ep.title}>${ep.title}</span>
      </label>
      <span class="row-end">${held ? html`<span class="muted">in batch</span>` : ep.on_disk ? html`<span class="state ok">on disk</span>` : null}</span>
    </li>`;
    })}`;
}

function PickReleases({ entry, onBack }) {
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [feedId, setFeedId] = useState(null);
  const [picked, setPicked] = useState(new Set());
  const [adding, setAdding] = useState(false);
  const choose = (group) => { setFeedId(group.feed_id); setPicked(defaultPick(group)); };
  useEffect(() => {
    let live = true;
    apiFetch('/api/add/search', { method: 'POST', json: { anilist_id: entry.anilist_id }, quiet: true })
      .then(body => {
        if (!live) return;
        setResult(body);
        const pre = body.preselect && body.feeds.find(g => g.feed_id === body.preselect);
        if (pre) choose(pre);
      })
      .catch(err => { if (live) setError(err.message); });
    return () => { live = false; };
  }, [entry.anilist_id]);
  const toggle = (group, release) => {
    // Episodes come from one feed: ticking one from another feed moves the whole selection there.
    if (group.feed_id !== feedId) { choose(group); return; }
    setPicked(set => { const next = new Set(set); if (next.has(release.item_id)) next.delete(release.item_id); else next.add(release.item_id); return next; });
  };
  const group = result && result.feeds.find(g => g.feed_id === feedId);
  const counts = group ? coverage(group, picked) : null;
  const add = async () => {
    setAdding(true);
    try {
      const body = await apiFetch('/api/add/confirm', { method: 'POST', json: { anilist_id: entry.anilist_id, feed_id: feedId, item_ids: [...picked] } });
      const parts = [`Added ${entry.title}.`];
      if (body.started) parts.push(`${body.started} download${body.started === 1 ? '' : 's'} started.`);
      if (body.errors && body.errors.length) parts.push(body.errors.join(' '));
      toast(parts.join(' '), body.errors && body.errors.length ? 'info' : 'success');
      go(body.show_id);
    } catch (err) { setAdding(false); }
  };
  const art = { id: `al-${entry.anilist_id}`, title: entry.title, cover: entry.cover };
  return html`<div class="stack">
    <div class="add-picked">
      <button type="button" class="btn plain icon-only sm" onClick=${onBack} aria-label="Pick another show" title="Pick another show"><${Icon} name="back" /></button>
      <div class="sr-poster"><${Poster} show=${art} dim=${false} /></div>
      <div class="grow">
        <h2 class="section-title truncate" title=${entry.title}>${entry.title}</h2>
        <p class="muted">${[entry.list_label, AIR_LABELS[entry.status] || null, entry.total_episodes ? `${entry.total_episodes} episodes` : null].filter(Boolean).join(' · ')}</p>
      </div>
    </div>
    ${error ? html`<p class="error-text">${error}</p>` : null}
    ${!result && !error ? html`<div class="card card-pad row"><${Spinner} small /><span class="muted">Searching your feeds for this show. This takes a few seconds.</span></div>` : null}
    ${result ? html`
      <${Notes} result=${result} />
      <div class="card list-card stack-sm">
        <p class="field-hint">Pick the feed to download from: its batch is selected when it has one, otherwise all of its episodes, and you can untick what you don't want. A batch is downloaded whole. Future episodes come from the same feed.</p>
        <ul class="list">
          ${result.feeds.length
            ? result.feeds.map(g => html`<${FeedGroup} key=${g.feed_id} group=${g} active=${g.feed_id === feedId} picked=${picked} onFeed=${choose} onToggle=${toggle} />`)
            : html`<li class="list-empty">None of your feeds has releases for this show. Add a feed that carries it on the RSS Feeds page, then try again.</li>`}
        </ul>
      </div>
      <div class="add-bar">
        <span class="muted">${group ? `${counts.covered.size} of ${counts.offered.size} episode${counts.offered.size === 1 ? '' : 's'} from ${group.feed_name}` : 'No feed selected'}</span>
        <button type="button" class="btn primary" disabled=${!group || adding} onClick=${add}>${adding ? html`<${Spinner} small />` : html`<${Icon} name="plus" small />`}<span>Add show</span></button>
      </div>` : null}
  </div>`;
}

export function Add() {
  const [entry, setEntry] = useState(null);
  return html`<div class="page wide">
    ${entry ? html`<${PickReleases} key=${entry.anilist_id} entry=${entry} onBack=${() => setEntry(null)} />` : html`<${PickEntry} onPick=${setEntry} />`}
  </div>`;
}
