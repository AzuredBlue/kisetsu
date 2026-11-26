// Adding a show by hand: pick an entry of the user's AniList list (#add), then the feed and the episodes to download (#add/<id>).
import { html, useEffect, useLayoutEffect, useMemo, useRef, useState } from '../ui.js';
import { apiFetch } from '../api.js';
import { go } from '../router.js';
import { useResource } from '../store.js';
import { addList } from '../data.js';
import { Icon, Spinner } from '../components/Icon.js';
import { Poster } from '../components/ShowSummary.js';
import { toast } from '../components/Toast.js';
import { LoadError } from '../components/LoadError.js';
import { plural, relativeTime } from '../format.js';

const version = (v) => (v > 1 ? ` v${v}` : '');
const fold = (text) => String(text || '').normalize('NFKD').replace(/[̀-ͯ]/g, '').toLowerCase();
const AIR_LABELS = { RELEASING: 'Releasing', FINISHED: 'Finished', NOT_YET_RELEASED: 'Not yet released', HIATUS: 'On hiatus', CANCELLED: 'Cancelled' };

// Watching and Rewatching come first, then Planning, Paused, Dropped and Completed.
const LIST_ORDER = { CURRENT: 0, REPEATING: 0, PLANNING: 1, PAUSED: 2, DROPPED: 3, COMPLETED: 4 };
const listRank = (entry) => LIST_ORDER[entry.list_status] ?? 5;
const FOLLOWING = 'Already following';
const LIST_PILLS = { CURRENT: 'pill-info', REPEATING: 'pill-info' };
const listPill = (entry) => entry.list_label ? html`<span class=${`pill ${LIST_PILLS[entry.list_status] || 'pill-idle'}`}>${entry.list_label}</span>` : null;

// Entries by list status, in list order; those already followed come last as shortcuts to their page.
function grouped(entries) {
  const groups = new Map();
  [...entries].sort((a, b) => listRank(a) - listRank(b)).forEach(entry => {
    const label = entry.followed ? FOLLOWING : listRank(entry) === 0 ? 'Watching' : entry.list_label || 'Other';
    if (!groups.has(label)) groups.set(label, { label, rank: entry.followed ? 9 : listRank(entry), entries: [] });
    groups.get(label).entries.push(entry);
  });
  return [...groups.values()].sort((a, b) => a.rank - b.rank || a.label.localeCompare(b.label));
}

function EntryRow({ entry, onOpen }) {
  const facts = [AIR_LABELS[entry.status] || null, entry.season, entry.total_episodes ? `${entry.total_episodes} ep` : null].filter(Boolean).join(', ');
  const art = { id: `al-${entry.anilist_id}`, title: entry.title, cover: entry.cover };
  return html`<li><button type="button" class="add-entry" onClick=${() => onOpen(entry)}>
    <div class="sr-poster"><${Poster} show=${art} dim=${false} small /></div>
    <div class="grow">
      <div class="add-entry-title truncate" title=${entry.title}>${entry.title}</div>
      <div class="add-entry-meta">${listPill(entry)}<span class="muted truncate">${facts}</span></div>
    </div>
    ${entry.followed ? html`<span class="muted">Following</span>` : null}
    <${Icon} name="forward" small />
  </button></li>`;
}

function PickEntry({ list, text, setText, onOpen }) {
  const [syncing, setSyncing] = useState(false);
  // Reads the AniList list now, then asks for it again.
  const sync = async () => {
    setSyncing(true);
    try { await apiFetch('/api/refresh', { method: 'POST' }); await list.reload(); } catch (err) { /* shown by apiFetch */ }
    setSyncing(false);
  };
  const data = list.data;
  const found = useMemo(() => {
    if (!data) return [];
    const words = fold(text).split(/\s+/).filter(Boolean);
    return !words.length ? data.entries : data.entries.filter(e => {
      const hay = fold([e.title, ...(e.aliases || [])].join(' '));
      return words.every(w => hay.includes(w));
    });
  }, [data, text]);
  const groups = useMemo(() => grouped(found), [found]);
  const first = groups.length ? groups[0].entries[0] : null;
  const searching = text.trim() !== '';
  const syncButton = html`<button type="button" class="btn sm" disabled=${syncing} onClick=${sync}>${syncing ? html`<${Spinner} small />` : null}Sync now</button>`;
  const error = !data && list.error;
  return html`<div class="stack add-step">
    <section class="add-section">
      <div class="add-search">
        <${Icon} name="search" small />
        <input class="field" type="search" placeholder="Search your AniList list" value=${text} onInput=${(e) => setText(e.target.value)} onKeyDown=${(e) => { if (e.key === 'Enter' && first) onOpen(first); }} autofocus aria-label="Search your AniList list" />
      </div>
      ${data && data.fetched_at ? html`<p class="field-hint add-hint">${searching ? `${plural(found.length, 'match', 'matches')}. ` : ''}AniList list read ${relativeTime(data.fetched_at, Date.now())}. Kisetsu adds Planning shows itself once their season is the current or next one. ${syncButton}</p>` : null}
    </section>
    ${error ? (error.status === 400 ? html`<div class="add-note info"><${Icon} name="info" small /><span>${error.message}</span>${syncButton}</div>` : html`<${LoadError} what="your AniList list" error=${error} reload=${list.reload} />`) : null}
    ${error ? null : html`<div class="card feeds-card"><ul class="feed-list">
      ${!data ? html`<li class="add-empty"><${Spinner} small /> Reading your list…</li>` : null}
      ${data && !groups.length ? html`<li class="add-empty">
        ${text ? 'Nothing on your list matches.' : 'Your list has no entries to add.'}
        <p class="field-hint">Entries you are watching, rewatching, paused or dropped show here, but not Completed ones. Planning entries are added by themselves once their season is the current or next one. To add something else, put it on your Watching list on AniList, then press Sync now.</p>
      </li>` : null}
      ${groups.map(group => html`<li class="list-head" key=${`head-${group.label}`}><span>${group.label}</span><span class="muted tabular">${group.entries.length}</span></li>${group.entries.map(entry => html`<${EntryRow} key=${entry.anilist_id} entry=${entry} onOpen=${onOpen} />`)}`)}
    </ul></div>`}
  </div>`;
}

function Notes({ result }) {
  const notes = [];
  const folder = result.folder;
  // The backend's own note already names the folder when no feed matches its files.
  if (folder && !result.note) {
    const one = folder.files === 1;
    const where = html`<span class="mono">${folder.path}</span>`;
    const check = html`qBittorrent checks ${one ? 'that file' : 'those files'} instead of downloading ${one ? 'it' : 'them'} again.`;
    notes.push(['info', result.preselect
      ? html`${plural(folder.files, 'file')} for this show ${one ? 'is' : 'are'} already in ${where}. Kisetsu picked the feed ${one ? 'it' : 'they'} came from, and ${check}`
      : html`${plural(folder.files, 'file')} for this show ${one ? 'is' : 'are'} already in ${where}. The show uses that folder, and ${check}`]);
  }
  if (result.watched) notes.push(['info', `You watched up to episode ${result.watched} on AniList, so those episodes were not searched for or ticked.`]);
  if (result.note) notes.push(['warn', result.note]);
  if (result.unsearchable && result.unsearchable.length) notes.push(['info', `Only the RSS of ${result.unsearchable.join(', ')} was read: ${result.unsearchable.length === 1 ? 'its site' : 'their sites'} cannot be searched.`]);
  if (result.stopped) notes.push(['warn', `The search stopped early: ${result.stopped}`]);
  if (!notes.length) return null;
  return html`<div class="stack-sm">${notes.map(([tone, text], i) => html`<p key=${i} class=${`add-note ${tone}`}><${Icon} name=${tone === 'warn' ? 'alert' : 'info'} small /><span>${text}</span></p>`)}</div>`;
}

// The episodes the feed offers, and those the ticked releases cover; a ticked batch covers its singles.
function coverage(group, picked) {
  const offered = new Set([...group.episodes.map(e => e.number), ...(group.batches || []).flatMap(b => b.numbers)]);
  const inBatch = new Set((group.batches || []).filter(b => picked.has(b.item_key)).flatMap(b => b.numbers));
  const covered = new Set([...inBatch, ...group.episodes.filter(e => picked.has(e.item_key)).map(e => e.number)]);
  return { offered, inBatch, covered };
}

// Picking a feed takes all it offers: its batches (the largest first, then those that add episodes), and singles for the rest.
// A batch that names its episodes is trusted over one that only says "Batch". Episodes up to `watched` are left out: a batch
// is taken only for unwatched episodes no single offers.
function pickAll(group, watched = 0) {
  const fresh = (n) => n > watched;
  const singles = new Set(group.episodes.filter(e => fresh(e.number)).map(e => e.number));
  const batches = (group.batches || []).filter(b => b.numbers.some(n => fresh(n) && (!watched || !singles.has(n))));
  const ranged = batches.filter(b => b.ranged);
  const taken = new Set();
  const ids = new Set();
  (ranged.length ? ranged : batches).forEach(b => {
    if (b.numbers.some(n => taken.has(n))) return;
    ids.add(b.item_key);
    b.numbers.forEach(n => taken.add(n));
  });
  group.episodes.filter(e => fresh(e.number) && !taken.has(e.number)).forEach(e => ids.add(e.item_key));
  return ids;
}

// What the ticked releases will do: episodes to download, and episodes whose files are already on disk. Each episode comes
// from one release, a ticked batch before a single, as the server takes them.
function outcome(group, picked, watched) {
  const seen = new Set();
  let fresh = 0;
  let disk = 0;
  if (!group) return { fresh, disk };
  (group.batches || []).filter(b => picked.has(b.item_key)).forEach(b => {
    const unwatched = b.numbers.filter(n => n > (watched || 0));
    const own = (unwatched.length ? unwatched : b.numbers).filter(n => !seen.has(n));
    own.forEach(n => seen.add(n));
    const held = Math.min(b.on_disk || 0, own.length);
    disk += held;
    fresh += own.length - held;
  });
  group.episodes.filter(e => picked.has(e.item_key) && !seen.has(e.number)).forEach(e => {
    seen.add(e.number);
    if (e.on_disk) disk += 1; else fresh += 1;
  });
  return { fresh, disk };
}

const span = (numbers) => (numbers.length > 1 ? `Ep ${numbers[0]}–${numbers[numbers.length - 1]}` : `Ep ${numbers[0]}`);

// The same release in two feeds has a different guid in each, so releases are told apart by name.
const identity = (title) => String(title).replace(/\.(mkv|mp4|avi|webm|m4v|mov|wmv|ts)$/i, '').trim().toLowerCase();
const releasesOf = (group) => [...(group.batches || []), ...group.episodes];
const identities = (group) => new Set(releasesOf(group).map(r => identity(r.title)));

// One line that sets a feed apart from the others.
function difference(group, groups) {
  const mine = identities(group);
  const others = groups.filter(g => g.feed_id !== group.feed_id).map(g => ({ name: g.feed_name, set: identities(g) }));
  const same = others.filter(o => o.set.size === mine.size && [...mine].every(k => o.set.has(k)));
  if (same.length) return { text: `Same releases as ${same.map(o => o.name).join(', ')}` };
  const larger = others.filter(o => o.set.size > mine.size && [...mine].every(k => o.set.has(k)));
  if (larger.length) return { text: `All of these are in ${larger.map(o => o.name).join(', ')}` };
  const only = [...mine].filter(k => !others.some(o => o.set.has(k))).length;
  if (only) return { text: `${plural(only, 'release')} only in this feed` };
  return { sample: (releasesOf(group)[0] || {}).title };
}

function FeedOption({ group, groups, active, onFeed }) {
  const { offered } = coverage(group, new Set());
  const batches = group.batches || [];
  const onDisk = Math.max(group.episodes.filter(e => e.on_disk).length, ...batches.map(b => b.on_disk || 0));
  const low = Math.min(...offered);
  const high = Math.max(...offered);
  const range = offered.size === 1 ? `Ep ${low}` : offered.size === high - low + 1 ? `Ep ${low}–${high}` : `${offered.size} of ${high - low + 1} episodes`;
  const stamps = releasesOf(group).map(r => Date.parse(r.published_at)).filter(Number.isFinite);
  const newest = stamps.length ? new Date(Math.max(...stamps)).toISOString() : null;
  const setApart = difference(group, groups);
  return html`<li><label class=${`feed-row add-feed${active ? ' selected' : ''}`}>
    <input type="radio" name="add-feed" checked=${active} onChange=${() => onFeed(group)} />
    <div class="feed-main">
      <div class="feed-name">${group.feed_name}</div>
      <div class="add-feed-facts tabular">
        <span>${range}</span>
        ${batches.length ? html`<span class="pill pill-idle">Batch</span>` : null}
        ${onDisk ? html`<span class="state ok">${onDisk} on disk</span>` : null}
        ${newest ? html`<span class="muted">newest ${relativeTime(newest, Date.now())}</span>` : null}
      </div>
      ${setApart.text ? html`<div class="add-feed-diff">${setApart.text}</div>` : html`<div class="add-feed-diff mono">${setApart.sample}</div>`}
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
function Releases({ group, picked, watched, onToggle }) {
  const { inBatch } = coverage(group, picked);
  const singles = group.episodes.filter(ep => !inBatch.has(ep.number));
  const hidden = group.episodes.length - singles.length;
  const ticked = (group.batches || []).filter(b => picked.has(b.item_key)).length;
  return html`<ul class="feed-list add-releases">
    ${(group.batches || []).map(batch => html`<${ReleaseRow} key=${`b:${batch.item_key}`} release=${batch} label=${span(batch.numbers)} checked=${picked.has(batch.item_key)} onToggle=${() => onToggle(batch)}>
      ${batch.on_disk ? html`<span class="state ok">${batch.on_disk} on disk</span>` : null}<span class="pill pill-idle">Batch</span>
    <//>`)}
    ${hidden ? html`<li class="add-hidden">Untick the ${ticked === 1 ? 'batch' : 'batches'} to pick from ${plural(hidden, 'single episode')}.</li>` : null}
    ${singles.map(ep => html`<${ReleaseRow} key=${`e:${ep.item_key}`} release=${ep} label=${`Ep ${ep.number}${version(ep.version)}`} checked=${picked.has(ep.item_key)} onToggle=${() => onToggle(ep)}>
      ${ep.on_disk ? html`<span class="state ok">on disk</span>` : watched && ep.number <= watched ? html`<span class="state">watched</span>` : null}
    <//>`)}
  </ul>`;
}

const NO_PICKS = new Set();

function PickReleases({ entry, onBack }) {
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [attempt, setAttempt] = useState(0);
  const [feedId, setFeedId] = useState(null);
  // Each feed keeps its own ticks, so going back to one finds them as they were left.
  const [picks, setPicks] = useState({});
  const [adding, setAdding] = useState(false);
  const choose = (group, watched) => {
    setFeedId(group.feed_id);
    setPicks(all => (all[group.feed_id] ? all : { ...all, [group.feed_id]: pickAll(group, watched || 0) }));
  };
  const picked = picks[feedId] || NO_PICKS;
  const retry = () => { setError(null); setResult(null); setAttempt(n => n + 1); };
  useEffect(() => {
    let live = true;
    apiFetch('/api/add/search', { method: 'POST', json: { anilist_id: entry.anilist_id }, quiet: true })
      .then(body => {
        if (!live) return;
        setResult(body);
        setFeedId(null);
        setPicks({});
        if (body.nothing_offered) { setFeedId(0); return; }
        // A lone feed is no choice to make.
        const pre = body.feeds.length === 1 ? body.feeds[0] : body.preselect && body.feeds.find(g => g.feed_id === body.preselect);
        if (pre) choose(pre, body.watched);
      })
      .catch(err => { if (live) setError(err); });
    return () => { live = false; };
  }, [entry.anilist_id, attempt]);
  const toggle = (release) => {
    setPicks(all => {
      const next = new Set(all[feedId]);
      if (next.has(release.item_key)) next.delete(release.item_key); else next.add(release.item_key);
      return { ...all, [feedId]: next };
    });
  };
  // The step never gets shorter: ticking a batch hides rows, and the clamped scroll would move the page under the pointer.
  const step = useRef(null);
  useLayoutEffect(() => { step.current.style.minHeight = `${step.current.offsetHeight}px`; }, [feedId, picks, result]);
  const group = result && !result.nothing_offered && result.feeds.find(g => g.feed_id === feedId);
  const counts = group ? coverage(group, picked) : null;
  const plan = outcome(group, picked, result && result.watched);
  const add = async () => {
    setAdding(true);
    try {
      const body = await apiFetch('/api/add/confirm', { method: 'POST', json: { anilist_id: entry.anilist_id, feed_id: feedId, item_keys: [...picked] } });
      const parts = [`Added ${entry.title}.`];
      // Episodes already on disk are added too, so qBittorrent can check their files.
      const checked = Math.min(plan.disk, body.started);
      if (body.started - checked) parts.push(`${plural(body.started - checked, 'download')} started.`);
      if (checked) parts.push(`${plural(checked, 'episode')} on disk checked by qBittorrent.`);
      if (body.errors && body.errors.length) parts.push(body.errors.join(' '));
      toast(parts.join(' '), body.errors && body.errors.length ? 'info' : 'success');
      go(body.show_id);
    } catch (err) { setAdding(false); }
  };
  const art = { id: `al-${entry.anilist_id}`, title: entry.title, cover: entry.cover };
  const summary = !result ? '' : result.nothing_offered || (group && !plan.fresh && !plan.disk) ? 'Future episodes only'
    : !group ? 'Pick a feed to see its releases'
    : plan.fresh && plan.disk ? `${plan.fresh} to download, ${plan.disk} on disk`
    : plan.fresh ? `${plan.fresh} to download`
    : `${plan.disk} on disk, nothing to download`;
  const upcoming = entry.status === 'NOT_YET_RELEASED';
  const feedName = result && result.nothing_offered ? (feedId === 0 ? 'Auto-discover' : (result.feeds.find(g => g.feed_id === feedId) || {}).feed_name) : group && group.feed_name;
  const blocked = adding || (result && result.nothing_offered ? feedId === null : !group);
  const bar = result && (result.nothing_offered || result.feeds.length) ? html`<div class="add-bar">
    <div class="add-outcome">
      <div class="add-summary">${summary}</div>
      ${feedName ? html`<div class="add-summary-feed">From ${feedName}</div>` : null}
    </div>
    <button type="button" class="btn primary" disabled=${blocked} onClick=${add}>${adding ? html`<${Spinner} small />` : html`<${Icon} name="plus" small />`}<span>${plan.fresh ? `Add and download ${plan.fresh}` : 'Add show'}</span></button>
  </div>` : null;
  return html`<div class="stack add-step" ref=${step}>
    <div class="add-picked">
      <div class="sr-poster"><${Poster} show=${art} dim=${false} small /></div>
      <div class="grow">
        <h2 class="section-title truncate" title=${entry.title}>${entry.title}</h2>
        <div class="add-entry-meta">${listPill(entry)}<span class="muted">${[AIR_LABELS[entry.status] || null, entry.total_episodes ? `${entry.total_episodes} episodes` : null].filter(Boolean).join(', ')}</span></div>
      </div>
    </div>
    ${error ? html`<${LoadError} what="the releases for this show" error=${error} reload=${retry} />` : null}
    ${!result && !error ? html`<div class="card card-pad row"><${Spinner} small /><span class="muted">Searching your feeds for this show. This takes a few seconds.</span></div>` : null}
    ${result ? html`
      <${Notes} result=${result} />
      ${result.nothing_offered ? html`
        <div class="add-cols solo">
          <section class="add-section">
            <div class="add-section-head"><h3 class="section-title">Feed</h3><span class="muted">${upcoming ? 'Nothing has been released yet.' : 'None of your feeds offers a release of this show right now.'}</span></div>
            <div class="card feeds-card"><ul class="feed-list">
              ${[{ feed_id: 0, feed_name: 'Auto-discover', hint: 'The first feed that posts an episode is used, then kept.' }, ...result.feeds.map(g => ({ ...g, hint: 'Only this feed is used.' }))].map(g => html`<li key=${g.feed_id}><label class=${`feed-row add-feed${g.feed_id === feedId ? ' selected' : ''}`}>
                <input type="radio" name="add-feed" checked=${g.feed_id === feedId} onChange=${() => setFeedId(g.feed_id)} />
                <div class="feed-main"><div class="feed-name">${g.feed_name}</div><div class="add-feed-diff">${g.hint}</div></div>
              </label></li>`)}
            </ul></div>
          </section>
        </div>`
      : result.feeds.length ? html`
        <div class="add-cols">
          <section class="add-section">
            <div class="add-section-head"><h3 class="section-title">Feed</h3><span class="muted">Downloads for this show come from this feed.</span></div>
            <div class="card feeds-card"><ul class="feed-list">
              ${result.feeds.map(g => html`<${FeedOption} key=${g.feed_id} group=${g} groups=${result.feeds} active=${g.feed_id === feedId} onFeed=${(chosen) => choose(chosen, result.watched)} />`)}
            </ul></div>
          </section>
          <section class="add-section">
            <div class="add-section-head">
              <h3 class="section-title">Releases</h3>
              ${group ? html`<span class="add-all">
                <button type="button" class="btn plain sm" disabled=${counts.covered.size === counts.offered.size} onClick=${() => setPicks(all => ({ ...all, [feedId]: pickAll(group) }))}>All</button>
                <button type="button" class="btn plain sm" disabled=${!picked.size} onClick=${() => setPicks(all => ({ ...all, [feedId]: new Set() }))}>None</button>
              </span>` : null}
            </div>
            <div class="card feeds-card">
              ${group ? html`<${Releases} key=${group.feed_id} group=${group} picked=${picked} watched=${result.watched} onToggle=${toggle} />` : html`<div class="add-empty">Pick a feed to see its releases.</div>`}
            </div>
          </section>
        </div>`
      : html`<div class="card empty">You have no RSS feeds yet. Add one on the <a class="link" href="#feeds">RSS Feeds</a> page, then try again.</div>`}
      ${bar}` : null}
  </div>`;
}

export function Add({ route }) {
  const list = useResource(addList);
  const [text, setText] = useState('');
  const scroll = useRef(0);
  const id = route.id;
  const entry = id && list.data ? list.data.entries.find(e => e.anilist_id === id) : null;
  const open = (picked) => {
    if (picked.followed) { go(picked.show_id); return; }
    scroll.current = document.getElementById('content').scrollTop;
    go(`add/${picked.anilist_id}`);
  };
  // The list comes back where it was left; a picked show starts at the top.
  useLayoutEffect(() => { document.getElementById('content').scrollTop = id ? 0 : scroll.current; }, [id]);
  useEffect(() => {
    if (!id) return undefined;
    const onKey = (e) => { if (e.key === 'Escape') go('add'); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [id]);
  useEffect(() => { if (entry && entry.followed) go(entry.show_id); }, [entry && entry.followed]);
  let body;
  if (!id) body = html`<${PickEntry} list=${list} text=${text} setText=${setText} onOpen=${open} />`;
  else if (entry && !entry.followed) body = html`<${PickReleases} key=${entry.anilist_id} entry=${entry} />`;
  else if (list.data) body = entry ? null : html`<div class="card empty">That show is not on your AniList list. <button type="button" class="link" onClick=${() => go('add')}>Back to the list</button></div>`;
  else if (list.error) body = html`<${LoadError} what="your AniList list" error=${list.error} reload=${list.reload} />`;
  else body = html`<div class="card card-pad row"><${Spinner} small /><span class="muted">Reading your list…</span></div>`;
  return html`<div class=${`page ${id ? 'add-two' : 'medium'}`}>${body}</div>`;
}
