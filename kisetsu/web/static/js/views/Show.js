import { html, useEffect, useLayoutEffect, useMemo, useState } from '../ui.js';
import { apiFetch } from '../api.js';
import { useResource } from '../store.js';
import { go, leaveTarget, useNow } from '../router.js';
import { applyShowPalette } from '../theme.js';
import { feedMatches, feeds as feedsData, settings as settingsData, shows as showsData, showPage } from '../data.js';
import { Icon, Spinner } from '../components/Icon.js';
import { confirmDialog } from '../components/Modal.js';
import { SaveBar, Seg } from '../components/Controls.js';
import { useDraft } from '../components/useDraft.js';
import { toast } from '../components/Toast.js';
import { EpisodeList, ReleaseList, useRowAction } from '../components/Episodes.js';
import { airSummary, deleteShow, dimmed, Poster, ProgressBar, StatusTag, togglePause } from '../components/ShowSummary.js';
import { localDateTime } from '../format.js';

// The values the side form starts from.
function initialForm(page) {
  return {
    feed_id: page.feed && ['pinned', 'locked'].includes(page.feed.state) ? page.feed.id : 0,
    save_folder: page.save_folder || page.save_path || '',
    category: page.category || '',
    seed_ratio: page.seed_ratio === null || page.seed_ratio === undefined ? '' : String(page.seed_ratio),
    aliases: (page.custom_aliases || []).join(', '),
  };
}

function feedHint(page) {
  const feed = page.feed;
  if (!feed) return page.released ? 'Auto: testing feeds by priority.' : 'Auto: first feed to post wins.';
  if (feed.state === 'locked') return `Locked to ${feed.name} after a download.`;
  if (feed.state === 'pinned') return 'Pinned by you.';
  if (feed.state === 'candidate') return `May move to ${feed.name}, which posted first.`;
  return page.status === 'waiting' ? 'Auto: waiting for a release.' : 'Auto: testing feeds by priority.';
}

function Header({ page, now }) {
  const air = airSummary(page, now);
  const alt = [page.title_romaji, page.title_english].find(t => t && t !== page.title);
  const subtitle = [alt, page.season].filter(Boolean).join(', ');
  const { done, total } = page.progress;
  const showProgress = done > 0 || total > 0 || !!page.next_airing;
  return html`<div class="show-head">
    <div class="show-head-top">
      <div class="grow">
        <div class="row">
          <h2 class="show-title truncate" title=${page.title}>${page.title}</h2>
          <a href=${`https://anilist.co/anime/${page.anilist_id}`} target="_blank" rel="noopener noreferrer" class="ext" title="Open on AniList" aria-label="Open on AniList"><${Icon} name="link" /></a>
        </div>
        ${subtitle ? html`<p class="muted truncate" style="margin-top:.25rem">${subtitle}</p>` : null}
      </div>
      <div class="row">
        ${page.status !== 'completed' ? html`<button type="button" class="btn sm" onClick=${() => togglePause(page)}><${Icon} name=${page.paused ? 'play' : 'pause'} small />${page.paused ? 'Resume' : 'Pause'}</button>` : null}
        <button type="button" class="btn sm danger" onClick=${async () => { if (await deleteShow(page)) go(leaveTarget()); }}>Delete</button>
      </div>
    </div>
    <div class="show-facts">
      <span class=${`pill ${{ working: 'pill-ok', testing: 'pill-warn', waiting: 'pill-info', upcoming: 'pill-info', paused: 'pill-idle', completed: 'pill-done' }[page.status]}`}>${page.status_label}</span>
      ${air.ep || air.info ? html`<span><b>${air.ep}</b> <span class=${`when ${air.tone}`} title=${air.at ? localDateTime(air.at) : undefined}>${air.info}</span></span>` : null}
      <span class="feed-fact muted" title=${page.feed ? page.feed.name : 'No feed'}><${Icon} name="feeds" small /><span class="truncate">${page.feed ? page.feed.name : 'No feed'}</span></span>
    </div>
    ${showProgress ? html`<div class="show-progress"><${ProgressBar} show=${page} /><span class="muted tabular">${done}/${total || '?'} downloaded</span></div>` : null}
  </div>`;
}

export function Show({ route }) {
  const id = route.id;
  const page = useResource(showPage(id));
  const matches = useResource(feedMatches(id));
  const feedsRes = useResource(feedsData);
  const settings = useResource(settingsData);
  const knownShows = useResource(showsData);
  const now = useNow();
  const [listTab, setListTab] = useState('episodes');
  const [sideTab, setSideTab] = useState('feed');
  const [saving, setSaving] = useState(false);
  const [searching, setSearching] = useState(false);
  const data = page.data;

  const hues = data?.accent_hues || (knownShows.data || []).find(sh => sh.id === id)?.accent_hues || null;
  const huesKey = hues ? hues.join(',') : '';
  useLayoutEffect(() => {
    applyShowPalette(hues);
    return () => applyShowPalette(null);
  }, [huesKey]);

  useEffect(() => { setListTab('episodes'); }, [id]);
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape' && !document.querySelector('.modal-backdrop')) leave(); };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  });

  const initial = useMemo(() => (data ? initialForm(data) : {}), [data]);
  const form = useDraft(initial, id);
  const { value, dirty } = form;
  const actions = useRowAction(id, () => { page.reload(); matches.reload(); });

  const leave = async () => {
    if (dirty && !(await confirmDialog({ title: 'Discard unsaved changes?', body: html`<p>The changes on this page have not been saved.</p>`, confirm: 'Discard', danger: true }))) return;
    go(leaveTarget());
  };

  if (page.error && page.error.status === 404) return html`<div class="page"><div class="empty">This show is gone.</div><button class="btn" onClick=${() => go(leaveTarget())}>Back to shows</button></div>`;
  if (!data) {
    const known = (knownShows.data || []).find(sh => sh.id === id);
    return html`<div class="show-page">
      <div class="show-banner on-art"><button type="button" class="back" onClick=${leave} aria-label="Back" title="Back (Esc)"><${Icon} name="back" /></button></div>
      <div class="show-body"><div class="show-top">${known ? html`<div class="show-poster"><${Poster} show=${known} /></div>` : null}</div></div>
    </div>`;
  }

  const feedList = feedsRes.data || [];
  const save = async (override) => {
    const changed = override || form.changed();
    const body = {};
    let movesFeed = false;
    if ('feed_id' in changed) { body.feed_id = changed.feed_id; movesFeed = true; }
    if ('save_folder' in changed) body.save_folder = String(changed.save_folder).trim();
    if ('category' in changed) body.category = String(changed.category).trim();
    if ('seed_ratio' in changed) body.seed_ratio = changed.seed_ratio === '' ? -1 : parseFloat(changed.seed_ratio);
    if ('aliases' in changed) body.aliases = String(changed.aliases).split(',').map(a => a.trim()).filter(Boolean);
    const unfinished = data.counts.downloading + data.counts.replacing;
    const note = unfinished && movesFeed && body.feed_id > 0 ? 'Unfinished downloads from the old feed will be canceled and fetched again from the new one.' : '';
    if (movesFeed && data.feed && data.feed.state === 'locked' && body.feed_id !== data.feed.id) {
      const target = feedList.find(f => f.id === body.feed_id);
      const ok = await confirmDialog({
        title: `Move ${data.title}?`,
        body: html`<p>It is locked to <b>${data.feed.name}</b> because a release was recorded from it.</p>
          <p>Move it to ${target ? html`<b>${target.name}</b>` : 'auto-detect'} anyway? The next release downloaded will lock the show to that feed.</p>${note ? html`<p>${note}</p>` : null}`,
        confirm: 'Move',
      });
      if (!ok) return;
      body.release_lock = true;
    } else if (note) {
      const ok = await confirmDialog({ title: `Change the feed for ${data.title}?`, body: html`<p>${note}</p>`, confirm: 'Change feed' });
      if (!ok) return;
    }
    setSaving(true);
    try {
      const result = await apiFetch(`/api/shows/${id}/edit`, { method: 'POST', json: body });
      toast(result.message || 'Saved.', 'success');
      form.reset();
      page.reload();
      matches.reload();
    } catch (err) { /* shown by apiFetch */ }
    setSaving(false);
  };

  const rediscover = async () => {
    if (data.feed && data.feed.state === 'locked') {
      const ok = await confirmDialog({
        title: 'Reset to auto-discover?',
        body: html`<p><b>${data.title}</b> already downloaded a release from <b>${data.feed.name}</b>. That feed is locked and is what Kisetsu keeps using.</p><p>Resetting abandons it and discovers a feed again, which may move the show to a different one.</p>`,
        confirm: 'Reset',
      });
      if (!ok) return;
    }
    try {
      await apiFetch(`/api/shows/${id}/rediscover`, { method: 'POST' });
      toast('Set to auto-discover.', 'success');
      form.reset();
      page.reload();
    } catch (err) { /* shown by apiFetch */ }
  };

  const searchOlder = async () => {
    if (searching) return;
    setSearching(true);
    try {
      const { found } = await apiFetch(`/api/shows/${id}/search-releases`, { method: 'POST' });
      toast(found ? `Found ${found} past release${found === 1 ? '' : 's'}.${data.paused ? ' Resume the show to download them.' : ' Checking for episodes to download.'}` : 'Nothing older was found in the feeds.', found ? 'success' : 'info');
      if (found) { setListTab('releases'); page.reload(); matches.reload(); }
    } catch (err) { /* shown by apiFetch */ }
    setSearching(false);
  };

  const art = data.banner || data.cover;
  const matchData = matches.data;
  const numbering = (data.episode_offsets || []).find(o => data.feed && o.feed_id === data.feed.id);
  return html`<div class="show-page">
    <div class="show-backdrop" aria-hidden="true">${art ? html`<img src=${art} alt="" class=${data.banner ? 'sharp' : 'blur'} />` : null}</div>
    <div class="show-banner on-art">
      ${art ? html`<img src=${art} alt="" class=${data.banner ? 'sharp' : 'blur'} />` : null}
      <button type="button" class="back" onClick=${leave} aria-label="Back" title="Back (Esc)"><${Icon} name="back" /></button>
    </div>
    <div class="show-body">
      <div class="show-top">
        <div class="show-poster"><${Poster} show=${data} /></div>
        <${Header} page=${data} now=${now} />
      </div>
      <section class="show-lists">
        <div class="pane-head">
          <${Seg} label="List" value=${listTab} onChange=${setListTab} options=${[['episodes', 'Episodes'], ['releases', 'Releases']]} />
          <span class="row">
            <span class="muted tabular">${listTab === 'episodes' ? `${data.episodes.length} tracked` : matchData ? `${matchData.reduce((n, g) => n + g.matches.length, 0)} releases` : 'checking feed…'}</span>
            ${searching ? html`<span class="muted row"><${Spinner} small /> Searching…</span>` : html`<button type="button" class="link" onClick=${searchOlder} title="Ask the feeds' sites for this show's older releases, for episodes that are no longer in their RSS">Search older</button>`}
          </span>
        </div>
        <div class="card list-card">
          ${listTab === 'episodes'
            ? html`<${EpisodeList} episodes=${data.episodes} groups=${matchData} paused=${data.paused} now=${new Date(now)} actions=${actions} onSearch=${searchOlder} searching=${searching} />`
            : html`<${ReleaseList} groups=${matchData} loading=${!matchData} actions=${actions} onUseFeed=${(feedId) => save({ feed_id: feedId })} />`}
        </div>
      </section>
      <aside class="show-side">
        <div class="pane-head">
          <${Seg} label="Settings" value=${sideTab} onChange=${setSideTab} options=${[['feed', 'Feed'], ['download', 'Download']]} />
          ${sideTab === 'feed' ? html`<button type="button" class="link" onClick=${rediscover} title="Let Kisetsu find the feed again by itself">Reset to auto-discover</button>` : null}
        </div>
        <div class="card side-card">
          ${sideTab === 'feed' ? html`
            <div>
              <label for="show-feed-id" class="field-label">${!data.released ? 'Feed (not decided yet)' : data.status === 'testing' ? 'Feed (auto)' : 'Assigned feed'}</label>
              <select id="show-feed-id" class="field" value=${value('feed_id')} onChange=${(e) => form.set('feed_id', Number(e.target.value))}>
                <option value="0">Auto-discover</option>
                ${feedList.map(f => html`<option value=${f.id} selected=${f.id === value('feed_id')}>#${f.priority} ${f.name}</option>`)}
              </select>
              <p class="field-hint">${feedHint(data)}</p>
              ${numbering && numbering.offset > 0 ? html`<p class="field-hint">This feed numbers the season from episode ${numbering.offset + 1}.</p>` : null}
            </div>
            <div>
              <label for="show-aliases" class="field-label">Custom aliases</label>
              <input type="text" id="show-aliases" class="field mono" placeholder="Comma separated" ...${form.bind('aliases')} />
            </div>
            <div>
              <span class="field-label">Matched as</span>
              ${data.learned_name
                ? html`<input type="text" readonly class="field mono" value=${data.learned_name} title=${data.learned_name} onFocus=${(e) => e.target.select()} />`
                : html`<p class="field-hint" style="margin-top:0">Matched by name and aliases until a release appears.</p>`}
            </div>` : html`
            <div>
              <label for="show-save-path" class="field-label">Save path</label>
              <input type="text" id="show-save-path" class="field mono" ...${form.bind('save_folder')} placeholder=${`~/Anime/${data.title}`} />
            </div>
            <div class="pair">
              <div>
                <label for="show-category" class="field-label">Category</label>
                <input type="text" id="show-category" class="field mono" ...${form.bind('category')} placeholder=${(settings.data && settings.data.default_category) || 'anime'} />
              </div>
              <div>
                <label for="show-ratio" class="field-label">Seed ratio</label>
                <input type="number" step="0.1" min="0" id="show-ratio" class="field mono" ...${form.bind('seed_ratio')} placeholder=${(settings.data && settings.data.default_seed_ratio) || 1} />
              </div>
            </div>`}
        </div>
      </aside>
      <${SaveBar} open=${dirty} saving=${saving} onDiscard=${form.reset} onSave=${() => save()} />
    </div>
  </div>`;
}
