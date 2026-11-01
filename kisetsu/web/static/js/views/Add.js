// Adding a show by hand: pick an entry of the user's AniList list, then the feed and the episodes to download.
import { html, useEffect, useLayoutEffect, useMemo, useRef, useState } from '../ui.js';
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
  return html`<li><button type="button" class="add-entry" onClick=${() => (entry.followed ? go(entry.show_id) : onPick(entry))}>
    <div class="sr-poster"><${Poster} show=${art} dim=${false} /></div>
    <div class="grow">
      <div class="add-entry-title truncate" title=${entry.title}>${entry.title}</div>
      <div class="muted truncate">${facts}</div>
    </div>
    ${entry.followed ? html`<span class="muted">Following</span>` : null}
    <${Icon} name="forward" small />
  </button></li>`;
}

function PickEntry({ onPick }) {
  const [list, setList] = useState(null);
  const [error, setError] = useState(null);
  const [text, setText] = useState('');
  useEffect(() => {
    apiFetch('/api/add/list', { quiet: true }).then(setList).catch(err => setError(err.message));
  }, []);
  const shown = useMemo(() => {
    if (!list) return [];
    const words = fold(text).split(/\s+/).filter(Boolean);
    if (!words.length) return list.entries;
    return list.entries.filter(e => {
      const hay = fold([e.title, ...(e.aliases || [])].join(' '));
      return words.every(w => hay.includes(w));
    });
  }, [list, text]);
  return html`<div class="stack add-step">
    <section class="add-section">
      <div class="add-search">
        <${Icon} name="search" small />
        <input class="field" type="search" placeholder="Search your AniList list" value=${text} onInput=${(e) => setText(e.target.value)} autofocus aria-label="Search your AniList list" />
      </div>
      ${list && list.fetched_at ? html`<p class="field-hint add-hint">Your AniList list, read ${relativeTime(list.fetched_at, Date.now())}. Sync Now reads it again.</p>` : null}
    </section>
    ${error ? html`<p class="error-text">${error}</p>` : null}
    ${error ? null : html`<div class="card feeds-card"><ul class="feed-list">
      ${!list ? html`<li class="add-empty"><${Spinner} small /> Reading your list…</li>` : null}
      ${list && !shown.length ? html`<li class="add-empty">
        ${text ? 'Nothing on your list matches.' : 'Your list has no entries to add.'}
        <p class="field-hint">Entries you are watching, rewatching, paused or dropped show here, but not Completed ones. To add something else, put it on your list on AniList, then press Sync Now.</p>
      </li>` : null}
      ${shown.map(entry => html`<${EntryRow} key=${entry.anilist_id} entry=${entry} onPick=${onPick} />`)}
    </ul></div>`}
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

// Every release the feed needs to cover all it offers: the default pick, plus the singles its batches leave out.
function pickAll(group) {
  const ids = defaultPick(group);
  const { covered } = coverage(group, ids);
  group.episodes.filter(e => !covered.has(e.number)).forEach(e => ids.add(e.item_id));
  return ids;
}

const span = (numbers) => (numbers.length > 1 ? `Ep ${numbers[0]}–${numbers[numbers.length - 1]}` : `Ep ${numbers[0]}`);
const plural = (n, noun) => `${n} ${noun}${n === 1 ? '' : 's'}`;

function FeedOption({ group, active, picked, onFeed }) {
  const { offered, covered } = coverage(group, active ? picked : new Set());
  const batches = group.batches || [];
  const onDisk = Math.max(group.episodes.filter(e => e.on_disk).length, ...batches.map(b => b.on_disk || 0));
  const sample = (batches[0] || group.episodes[0] || {}).title;
  return html`<li><label class=${`feed-row add-feed${active ? ' selected' : ''}`}>
    <input type="radio" name="add-feed" checked=${active} onChange=${() => onFeed(group)} />
    <div class="feed-main">
      <div class="row">
        <span class="feed-name truncate">${group.feed_name}</span>
        <span class="feed-use tabular">
          ${onDisk ? html`<span class="state ok">${onDisk} on disk</span> · ` : null}${active ? `${covered.size} of ${offered.size}` : plural(offered.size, 'episode')}${batches.length ? ' · Batch' : ''}
        </span>
      </div>
      <div class="feed-url mono truncate" title=${sample}>${sample}</div>
    </div>
  </label></li>`;
}

function ReleaseRow({ release, label, checked, onToggle, children }) {
  return html`<li><label class="add-release">
    <input type="checkbox" checked=${checked} onChange=${onToggle} />
    <span class="ep-no">${label}</span>
    <span class="release-name mono truncate" title=${release.title}>${release.title}</span>
    ${children}
  </label></li>`;
}

// The singles a ticked batch covers would only repeat it, so they stay out until the batch is unticked.
function Releases({ group, picked, onToggle }) {
  const { inBatch } = coverage(group, picked);
  const singles = group.episodes.filter(ep => !inBatch.has(ep.number));
  const hidden = group.episodes.length - singles.length;
  const ticked = (group.batches || []).filter(b => picked.has(b.item_id)).length;
  return html`<ul class="feed-list add-releases">
    ${(group.batches || []).map(batch => html`<${ReleaseRow} key=${`b:${batch.item_id}`} release=${batch} label=${span(batch.numbers)} checked=${picked.has(batch.item_id)} onToggle=${() => onToggle(batch)}>
      ${batch.on_disk ? html`<span class="state ok">${batch.on_disk} on disk</span>` : null}<span class="pill pill-idle">Batch</span>
    <//>`)}
    ${hidden ? html`<li class="add-hidden">Untick the ${ticked === 1 ? 'batch' : 'batches'} to pick from ${plural(hidden, 'single episode')}.</li>` : null}
    ${singles.map(ep => html`<${ReleaseRow} key=${`e:${ep.item_id}`} release=${ep} label=${`Ep ${ep.number}${version(ep.version)}`} checked=${picked.has(ep.item_id)} onToggle=${() => onToggle(ep)}>
      ${ep.on_disk ? html`<span class="state ok">on disk</span>` : null}
    <//>`)}
  </ul>`;
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
        // A lone feed is no choice to make.
        const pre = body.feeds.length === 1 ? body.feeds[0] : body.preselect && body.feeds.find(g => g.feed_id === body.preselect);
        if (pre) choose(pre);
      })
      .catch(err => { if (live) setError(err.message); });
    return () => { live = false; };
  }, [entry.anilist_id]);
  const toggle = (release) => {
    setPicked(set => { const next = new Set(set); if (next.has(release.item_id)) next.delete(release.item_id); else next.add(release.item_id); return next; });
  };
  // The step never gets shorter: ticking a batch hides rows, and the clamped scroll would move the page under the pointer.
  const step = useRef(null);
  useLayoutEffect(() => { step.current.style.minHeight = `${step.current.offsetHeight}px`; }, [feedId, picked, result]);
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
  const summary = !group ? 'Pick a feed to see its releases'
    : counts.covered.size ? `${counts.covered.size} of ${plural(counts.offered.size, 'episode')}`
    : 'Future episodes only';
  return html`<div class="stack add-step" ref=${step}>
    <button type="button" class="btn plain sm add-back" onClick=${onBack}><${Icon} name="back" small /><span>Your list</span></button>
    <div class="add-picked">
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
      ${result.feeds.length ? html`
        <section class="add-section">
          <div class="add-section-head"><h3 class="section-title">Feed</h3><span class="muted">New episodes will only come from this feed.</span></div>
          <div class="card feeds-card"><ul class="feed-list">
            ${result.feeds.map(g => html`<${FeedOption} key=${g.feed_id} group=${g} active=${g.feed_id === feedId} picked=${picked} onFeed=${choose} />`)}
          </ul></div>
        </section>
        <section class="add-section">
          <div class="add-section-head">
            <h3 class="section-title">Releases</h3>
            ${group ? html`<span class="add-all">
              <button type="button" class="btn plain sm" disabled=${counts.covered.size === counts.offered.size} onClick=${() => setPicked(pickAll(group))}>All</button>
              <button type="button" class="btn plain sm" disabled=${!picked.size} onClick=${() => setPicked(new Set())}>None</button>
            </span>` : null}
          </div>
          <div class="card add-card">
            ${group ? html`<${Releases} key=${group.feed_id} group=${group} picked=${picked} onToggle=${toggle} />` : null}
            <div class="add-foot">
              <span class="add-summary truncate">${summary}${group ? html`<span class="add-summary-feed"> · ${group.feed_name}</span>` : null}</span>
              <button type="button" class="btn primary" disabled=${!group || adding} onClick=${add}>${adding ? html`<${Spinner} small />` : html`<${Icon} name="plus" small />`}<span>Add show</span></button>
            </div>
          </div>
        </section>`
      : html`<div class="card empty">None of your feeds has releases for this show. Add a feed that carries it on the <a class="link" href="#feeds">RSS Feeds</a> page, then try again.</div>`}` : null}
  </div>`;
}

export function Add() {
  const [entry, setEntry] = useState(null);
  return html`<div class="page medium">
    ${entry ? html`<${PickReleases} key=${entry.anilist_id} entry=${entry} onBack=${() => setEntry(null)} />` : html`<${PickEntry} onPick=${setEntry} />`}
  </div>`;
}
