    let allShows = [];
    let allFeeds = [];
    let currentSettings = {};
    let activeTab = 'shows';
    let currentInspectedShowId = null;

    // Muted status colours: dim text on near-card-tone backgrounds, so a tag reads as
    // a state marker rather than the brightest thing on the poster. All pairs keep
    // >= 4.5:1 contrast against their own background.
    const STATUS_CONFIG = {
      'FIXED': { label: 'Working', bg: 'st-ok' },
      'UNCONFIRMED': { label: 'Testing', bg: 'st-warn' },
      'UPCOMING': { label: 'Upcoming', bg: 'st-info' },
      'WAITING': { label: 'Waiting', bg: 'st-info' },
      'STALLED': { label: 'Stalled', bg: 'st-bad' },
      'COMPLETED': { label: 'Completed', bg: 'st-done' },
      'PAUSED': { label: 'Paused', bg: 'st-idle' },
    };

    const NAV_INACTIVE_CLASS = 'nav-item w-full flex items-center gap-2.5 px-2.5 py-1.5 rounded-md text-sm font-medium transition-colors text-zinc-400 hover:text-zinc-100 hover:bg-raised';
    const NAV_ACTIVE_CLASS = 'nav-item w-full flex items-center gap-2.5 px-2.5 py-1.5 rounded-md text-sm font-medium transition-colors nav-on [&>svg]:text-accent';
    const SEG_ACTIVE_CLASS = 'font-semibold seg-on';
    const SEG_INACTIVE_CLASS = 'font-medium text-zinc-400 hover:text-zinc-200';
    const TAB_TITLES = {
      shows: 'Shows', calendar: 'Weekly Calendar', history: 'Match History',
      feeds: 'RSS Feeds', logs: 'Logs', settings: 'Settings',
    };

    function toggleSidebar(open) {
      const sidebar = document.getElementById('sidebar');
      const backdrop = document.getElementById('sidebar-backdrop');
      if (!sidebar || !backdrop) return;
      sidebar.classList.toggle('-translate-x-full', !open);
      backdrop.classList.toggle('hidden', !open);
    }

    function switchTab(tab, { animate = true } = {}) {
      // Leaving a show page through the sidebar: ask first if the form has edits.
      if (currentInspectedShowId !== null) {
        if (!confirmDiscardShowChanges()) return;
        showInitialState = null;
        clearShowHash();
        leaveShowPage(tab);
        return;
      }
      if (activeTab === 'settings' && tab !== 'settings') {
        if (!confirmDiscardSettingsChanges()) return;
        settingsInitialState = null;
        updateSettingsSaveBar();
      }
      const changed = tab !== activeTab;
      activeTab = tab;
      document.querySelectorAll('nav button').forEach(b => {
        b.className = NAV_INACTIVE_CLASS;
      });
      const activeBtn = document.getElementById(`nav-${tab}`);
      if (activeBtn) {
        activeBtn.className = NAV_ACTIVE_CLASS;
      }
      const titleEl = document.getElementById('page-title');
      if (titleEl) titleEl.textContent = TAB_TITLES[tab] || '';
      toggleSidebar(false);

      document.getElementById('tab-shows').classList.toggle('hidden', tab !== 'shows');
      document.getElementById('tab-calendar').classList.toggle('hidden', tab !== 'calendar');
      document.getElementById('tab-history').classList.toggle('hidden', tab !== 'history');
      document.getElementById('tab-feeds').classList.toggle('hidden', tab !== 'feeds');
      document.getElementById('tab-settings').classList.toggle('hidden', tab !== 'settings');
      document.getElementById('tab-logs').classList.toggle('hidden', tab !== 'logs');

      if (animate && changed) {
        const entering = document.getElementById(`tab-${tab}`);
        if (entering) {
          entering.classList.remove('tab-enter');
          void entering.offsetWidth;
          entering.classList.add('tab-enter');
          entering.addEventListener('animationend', () => entering.classList.remove('tab-enter'), { once: true });
        }
      }

      if (tab === 'shows') loadShows();
      if (tab === 'calendar') { loadShows(); renderCalendar(); }
      if (tab === 'history') loadHistory();
      if (tab === 'feeds') loadFeeds();
      if (tab === 'settings') loadSettings();
      if (tab === 'logs') loadLogs();
    }

    function escapeHtml(str) {
      if (!str) return '';
      return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
    }

    // The server sends UTC; show times in the viewer's own timezone.
    function localTime(iso) {
      if (!iso) return '';
      const d = new Date(iso);
      if (isNaN(d)) return '';
      return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' });
    }

    function localDateTime(iso) {
      if (!iso) return '';
      const d = new Date(iso);
      if (isNaN(d)) return '';
      return d.toLocaleString([], { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
    }

    // `sticky` keeps the toast until it is dismissed; the return value updates or removes it.
    function showToast(message, type = 'info', { sticky = false } = {}) {
      const toast = document.createElement('div');
      const isError = type === 'error';
      const icons = {
        error: '<svg class="w-4 h-4 flex-shrink-0 text-rose-400" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>',
        success: '<svg class="w-4 h-4 flex-shrink-0 text-emerald-400" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>',
        info: '<svg class="w-4 h-4 flex-shrink-0 text-sky-400" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>',
      };
      const closeIcon = '<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2.2"><path stroke-linecap="round" stroke-linejoin="round" d="M6 6l12 12M18 6L6 18"/></svg>';

      // Neutral surface; the icon carries the meaning.
      toast.className = 'group border border-line bg-raised rounded-lg px-3 py-2 text-[13px] text-zinc-200 shadow-lg shadow-black/30 transition-[transform,opacity] duration-150 translate-y-1 opacity-0 flex items-center gap-2.5';
      toast.setAttribute('role', isError ? 'alert' : 'status');
      toast.innerHTML = `${icons[type] || icons.info}<span class="flex-1 min-w-0">${escapeHtml(message)}</span><button onclick="this.parentElement.remove()" aria-label="Dismiss" class="flex-shrink-0 text-zinc-500 hover:text-zinc-100 transition-colors ${isError || sticky ? '' : 'opacity-0 group-hover:opacity-100'}">${closeIcon}</button>`;

      document.getElementById('toast-container').appendChild(toast);
      setTimeout(() => { toast.classList.remove('translate-y-1', 'opacity-0'); }, 10);
      const dismiss = () => {
        toast.classList.add('opacity-0');
        setTimeout(() => toast.remove(), 250);
      };
      if (!sticky) setTimeout(dismiss, 3500);
      return {
        update: (text) => { const label = toast.querySelector('span'); if (label) label.textContent = text; },
        dismiss,
      };
    }

    const actionsInFlight = new Set();

    // Ignores a repeat call while the same action is still running, so a double
    // click cannot send two toggles or two deletes.
    async function once(key, action) {
      if (actionsInFlight.has(key)) return;
      actionsInFlight.add(key);
      try {
        return await action();
      } finally {
        actionsInFlight.delete(key);
      }
    }

    // A change that needs the cycle slot waits for a running check. If the answer
    // is slow and a check is the reason, say so instead of leaving a dead button.
    const BUSY_NOTICE_DELAY_MS = 800;

    function watchForRunningCheck() {
      let toast = null;
      let ticker = null;
      let stopped = false;
      const timer = setTimeout(async () => {
        try {
          const st = await (await fetch('/api/status')).json();
          if (stopped || !st.is_running_cycle) return;
          const baseSeconds = st.cycle_seconds || 0;
          const seenAt = Date.now();
          const text = () => `Waiting for the ${st.cycle_label || 'background check'} to finish (${baseSeconds + Math.round((Date.now() - seenAt) / 1000)}s). Your change applies right after.`;
          toast = showToast(text(), 'info', { sticky: true });
          ticker = setInterval(() => toast.update(text()), 1000);
        } catch {
          // The notice is a courtesy; the request itself carries on.
        }
      }, BUSY_NOTICE_DELAY_MS);
      return () => {
        stopped = true;
        clearTimeout(timer);
        clearInterval(ticker);
        if (toast) toast.dismiss();
      };
    }

    async function apiFetch(url, options = {}) {
      const { notice = true, ...fetchOptions } = options;
      const method = String(fetchOptions.method || 'GET').toUpperCase();
      const stopWatching = method !== 'GET' && notice ? watchForRunningCheck() : null;
      try {
        const res = await fetch(url, fetchOptions);
        let data = {};
        const text = await res.text();
        try {
          data = text ? JSON.parse(text) : {};
        } catch {
          data = { detail: text || res.statusText };
        }
        if (!res.ok) {
          const err = new Error(data.detail || data.message || `HTTP ${res.status}: ${res.statusText}`);
          err.status = res.status;
          throw err;
        }
        return data;
      } finally {
        if (stopWatching) stopWatching();
      }
    }

    // `quiet` is for the reload that follows a change on the server: a failure there is not worth a toast.
    async function loadShows({ quiet = false } = {}) {
      try {
        const [showsData, feedsData, settingsData] = await Promise.all([
          apiFetch('/api/shows'),
          apiFetch('/api/feeds'),
          apiFetch('/api/settings')
        ]);
        if (renderHold) await renderHold;
        allShows = showsData;
        allFeeds = feedsData;
        currentSettings = settingsData;
        const savedColor = settingsData.accent_color || DEFAULT_ACCENT;
        const savedTint = settingsData.accent_tint || 'subtle';
        if (!accentPreviewing && (savedColor !== userAccent.color || savedTint !== userAccent.tint || !document.getElementById('user-accent'))) {
          applyUserAccent(savedColor, savedTint);
        }
        renderShows();
        renderCalendar();
        updateStatus();
        syncShowPageHeader();
        warmAccents();
      } catch (err) {
        if (!quiet) showToast(`Failed loading data: ${err.message || err}`, 'error');
      }
    }

    let currentShowView = 'grid';
    try { currentShowView = localStorage.getItem('show_view_mode') === 'list' ? 'list' : 'grid'; } catch (err) {}

    function setShowView(mode) {
      currentShowView = mode === 'list' ? 'list' : 'grid';
      try { localStorage.setItem('show_view_mode', currentShowView); } catch (err) {}
      renderShows(true);
    }

    function updateViewButtonStyles() {
      ['grid', 'list'].forEach(m => {
        const btn = document.getElementById(`view-btn-${m}`);
        if (!btn) return;
        btn.className = m === currentShowView ? SEG_ACTIVE_CLASS : SEG_INACTIVE_CLASS;
      });
    }

    let currentSortMode = localStorage.getItem('show_sort_mode') || 'airing';

    function setSortMode(mode) {
      currentSortMode = mode;
      localStorage.setItem('show_sort_mode', mode);
      updateSortButtonStyles();
      renderShows(true);
    }

    function updateSortButtonStyles() {
      const modes = ['airing', 'title', 'default'];
      modes.forEach(m => {
        const btn = document.getElementById(`sort-btn-${m}`);
        if (!btn) return;
        btn.className = m === currentSortMode ? SEG_ACTIVE_CLASS : SEG_INACTIVE_CLASS;
      });
    }

    function isShowCompleted(show) {
      return (show.status || '').toUpperCase() === 'COMPLETED';
    }

    function sortShowsList(list) {
      const copy = [...list];

      copy.sort((a, b) => {
        if (currentSortMode === 'airing') {
          const now = Date.now();
          const aTime = a.next_airing_at ? new Date(a.next_airing_at).getTime() : Infinity;
          const bTime = b.next_airing_at ? new Date(b.next_airing_at).getTime() : Infinity;

          const aUpcoming = aTime > now && aTime !== Infinity;
          const bUpcoming = bTime > now && bTime !== Infinity;

          if (aUpcoming && !bUpcoming) return -1;
          if (!aUpcoming && bUpcoming) return 1;

          if (aTime !== bTime) return aTime - bTime;

          return (a.display_name || '').localeCompare(b.display_name || '');
        } else if (currentSortMode === 'title') {
          return (a.display_name || '').localeCompare(b.display_name || '');
        } else {
          return (a.id || 0) - (b.id || 0);
        }
      });
      return copy;
    }

    // A card grid is only rebuilt when its markup changed, so coming back from a show
    // (or a refresh that found nothing new) does not recreate every poster.
    const gridDrawn = new Map();
    function setGridHtml(grid, html) {
      if (gridDrawn.get(grid.id) === html) return;
      gridDrawn.set(grid.id, html);
      grid.innerHTML = html;
    }

    // Cards are 2:3 posters, at least 215px wide. Pick the number of columns that fits that
    // floor, so the cards then stretch to fill the row with nothing left over on the right.
    function fitShowGrids() {
      const wide = window.matchMedia('(min-width: 1024px)').matches;
      document.querySelectorAll('.show-grid').forEach(grid => {
        const width = grid.clientWidth;
        if (!wide || !width) {
          grid.style.gridTemplateColumns = '';
          return;
        }
        const gap = parseFloat(getComputedStyle(grid).columnGap) || 20;
        const cols = Math.max(2, Math.floor((width + gap) / (215 + gap)));
        grid.style.gridTemplateColumns = `repeat(${cols}, minmax(0, 1fr))`;
      });
    }

    // `animate` is only set for user-triggered sort/view changes, not the periodic refresh.
    function renderShows(animate = false) {
      updateSortButtonStyles();
      updateViewButtonStyles();
      const isList = currentShowView === 'list';
      const renderItem = isList ? renderShowRow : renderShowCard;
      // Completed shows sit in their own section, so they are pulled out first
      // rather than relying on them to be filtered out of Releasing/Planned.
      const completedShows = sortShowsList(allShows.filter(s => isShowCompleted(s)));
      const activeShows = allShows.filter(s => !isShowCompleted(s));
      const releasingShows = sortShowsList(activeShows.filter(s => s.is_released));
      const plannedShows = sortShowsList(activeShows.filter(s => !s.is_released));

      document.getElementById('badge-total-shows').textContent = allShows.length;
      document.getElementById('header-count-releasing').textContent = `${releasingShows.length}`;
      document.getElementById('header-count-planned').textContent = `${plannedShows.length}`;
      document.getElementById('header-count-completed').textContent = `${completedShows.length}`;

      const gridReleasing = document.getElementById('grid-releasing');
      const gridPlanned = document.getElementById('grid-planned');
      const gridCompleted = document.getElementById('grid-completed');

      [gridReleasing, gridPlanned, gridCompleted].forEach(grid => {
        grid.classList.toggle('show-list', isList);
        grid.classList.toggle('show-grid', !isList);
      });

      setGridHtml(gridReleasing, releasingShows.map(renderItem).join('') || '<div class="col-span-full py-6 text-center text-zinc-500 text-sm">No currently releasing anime.</div>');
      setGridHtml(gridPlanned, plannedShows.map(renderItem).join('') || '<div class="col-span-full py-6 text-center text-zinc-500 text-sm">No planned upcoming anime.</div>');
      setGridHtml(gridCompleted, completedShows.map(renderItem).join('') || '<div class="col-span-full py-6 text-center text-zinc-500 text-sm">No completed anime.</div>');

      // Releasing is always shown so the page is never blank; the trailing
      // sections are noise when they have nothing in them.
      document.getElementById('section-completed').classList.toggle('hidden', completedShows.length === 0);
      document.getElementById('section-planned').classList.toggle('hidden', plannedShows.length === 0);
      fitShowGrids();
      if (animate && !window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
        [gridReleasing, gridPlanned, gridCompleted].forEach(grid => {
          grid.classList.remove('show-swap');
          grid.getBoundingClientRect();
          grid.classList.add('show-swap');
          // Dropped again so a later background refresh doesn't replay it.
          setTimeout(() => grid.classList.remove('show-swap'), 600);
        });
      }
    }

    function formatEpisodeCountdown(targetDateStr) {
      if (!targetDateStr) return null;
      const target = new Date(targetDateStr).getTime();
      const now = Date.now();
      const diffSec = Math.floor((target - now) / 1000);

      if (diffSec <= 0) {
        return 'Aired';
      }

      const days = Math.floor(diffSec / 86400);
      const hours = Math.floor((diffSec % 86400) / 3600);
      const mins = Math.floor((diffSec % 3600) / 60);

      if (days >= 7) {
        return `${days}d`;
      }
      if (days > 0) {
        return `${days}d ${hours}h`;
      }
      if (hours > 0) {
        return `${hours}h ${mins}m`;
      }
      return `${Math.max(1, mins)}m`;
    }

    // "in 2d 1h" / "Aired"; falls back to the local date when there is no countdown.
    function formatCardCountdown(cd, dateStr) {
      if (cd === 'Aired') return 'Aired';
      return cd ? `in ${cd}` : (dateStr || '');
    }

    // How full the progress bar is. With a known season total it is downloaded/total.
    // While the total is unknown it is downloaded out of the episodes out so far plus the
    // one still to come, so a running show never looks finished and a backlog shows.
    function progressFraction(show) {
      const downloaded = show.downloaded_episodes_count || 0;
      const total = show.total_episodes || 0;
      if (total > 0) return Math.min(1, downloaded / total);
      const next = show.next_airing_episode;
      let aired = downloaded;
      if (next) {
        const at = show.next_airing_at ? Date.parse(show.next_airing_at) : NaN;
        aired = (!isNaN(at) && at <= Date.now()) ? next : next - 1;
      }
      return Math.min(1, downloaded / (Math.max(aired, downloaded) + 1));
    }

    // A tag only says what the section and the countdown don't already say, so Working
    // (the normal state) gets none, nor do Upcoming in Planned or Completed in Completed.
    function showStatusChip(label, cfg) {
      const tagTips = {
        Testing: 'No release seen yet. Trying the feeds by priority.',
        Waiting: 'Aired, but no release has turned up yet. It is grabbed as soon as a feed carries it.',
        Stalled: 'The download has stopped making progress.',
        Paused: 'Monitoring is paused for this show.',
      };
      return tagTips[label]
        ? `<span class="px-1.5 py-0.5 inline-flex items-center rounded text-xs font-medium border ${cfg.bg}" title="${tagTips[label]}">${label}</span>`
        : '';
    }

    function pauseIconSvg(isPaused) {
      return isPaused
        ? `<svg class="w-3.5 h-3.5 ml-0.5" fill="currentColor" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>`
        : `<svg class="w-3.5 h-3.5" fill="currentColor" viewBox="0 0 24 24"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>`;
    }

    // One compact row for the list view: the same facts as the card, on a single line.
    function renderShowRow(show) {
      const { isPaused, isCompleted, statusKey, cfg, label } = resolveShowStatus(show);
      const downloaded = show.downloaded_episodes_count || 0;
      const total = show.total_episodes || 0;
      const { epLabel, airInfo, airClass, countdownAttr } = showAirSummary(show, statusKey);
      const showBar = total > 0 || downloaded > 0 || !!show.next_airing_episode;
      const pct = showBar ? Math.round(progressFraction(show) * 100) : 0;
      const progressText = `${downloaded}/${total || '?'}`;
      const isDimmed = isPaused || isCompleted;
      const dimClass = isDimmed ? 'opacity-80 grayscale-[35%]' : '';
      const name = escapeHtml(show.display_name);
      const feedName = show.current_feed_name ? escapeHtml(show.current_feed_name) : 'No feed';
      const barClass = isDimmed ? 'bg-zinc-500' : 'bg-accent';
      const art = show.cover_image
        ? `<img src="${escapeHtml(show.cover_image)}" alt="${name}" class="absolute inset-0 w-full h-full object-cover ${dimClass}" loading="lazy" onerror="this.onerror=null;this.style.display='none'">`
        : '';
      const nextDate = localDateTime(show.next_airing_at);
      const btn = 'w-7 h-7 rounded-md flex items-center justify-center text-zinc-400 transition-colors active:scale-90';

      return `
        <div onclick="openShowPage(${show.id})" onpointerenter="prefetchShowPage(${show.id})" class="show-row cursor-pointer">
          <div data-poster-id="${show.id}" class="sr-poster on-art relative aspect-[2/3] rounded bg-zinc-900 overflow-hidden">${art}</div>

          <div class="sr-title">
            <h3 class="text-sm font-medium text-zinc-200 truncate" title="${name}">${name}</h3>
            <div class="sr-feed-sub text-xs text-zinc-500 truncate" title="${feedName}">${feedName}${showProblemParts(show).map(part => `<span class="ml-1.5">${part}</span>`).join('')}</div>
          </div>

          <div class="sr-air text-[13px]">
            <span class="font-medium text-zinc-300">${epLabel}</span>
            <span class="show-countdown font-medium tabular-nums ${airClass}" ${countdownAttr} title="${nextDate}">${airInfo}</span>
          </div>

          <div class="sr-feed text-xs text-zinc-500" title="${feedName}">
            <svg class="w-3 h-3 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M6 5c7.18 0 13 5.82 13 13M6 11a7 7 0 017 7m-6 0a1 1 0 11-2 0 1 1 0 012 0z"/></svg>
            <span class="truncate">${feedName}</span>
          </div>

          <div class="sr-progress" title="${progressText} downloaded">
            <div class="h-[5px] flex-1 rounded-full bg-line-soft overflow-hidden"><div class="h-full ${barClass}" style="width:${pct}%"></div></div>
            <span class="text-xs tabular-nums text-zinc-400 text-right">${progressText}</span>
          </div>

          <div class="sr-chip">${showStatusChip(label, cfg)}</div>

          <div class="sr-actions">
            ${!isCompleted ? `<button onclick="event.stopPropagation(); togglePauseShow(${show.id})" class="${btn} hover:bg-raised hover:text-zinc-100" title="${isPaused ? 'Resume monitoring' : 'Pause monitoring'}">${pauseIconSvg(isPaused)}</button>` : ''}
            <button onclick="event.stopPropagation(); deleteShow(${show.id})" class="${btn} hover:bg-rose-600 hover:text-white" title="Remove show">
              <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2.2"><path stroke-linecap="round" stroke-linejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/></svg>
            </button>
          </div>
        </div>
      `;
    }

    // "2 failed" / "1 missed" parts for a show whose episodes need attention.
    function showProblemParts(show) {
      const failed = show.failed_episodes_count || 0;
      const missed = show.missed_episodes_count || 0;
      const parts = [];
      if (failed) parts.push(`<span class="text-rose-400" title="${failed} episode(s) could not be downloaded. Open the show to see why.">${failed} failed</span>`);
      if (missed) parts.push(`<span class="text-amber-400" title="${missed} aired episode(s) were not found in any feed.">${missed} missed</span>`);
      return parts;
    }

    // Up to two initials, for a poster that has no art.
    function showInitials(name) {
      const words = String(name || '').split(/\s+/).map(w => w.replace(/[^\p{L}\p{N}]/gu, '')).filter(Boolean);
      return words.slice(0, 2).map(w => Array.from(w)[0].toUpperCase()).join('');
    }

    function renderShowCard(show) {
      const { isPaused, isCompleted, statusKey, cfg, label } = resolveShowStatus(show);

      // As in Sonarr, the poster stays clean: the bar sits under it and the episode, the
      // countdown and the count go in the lines below the title.
      const downloaded = show.downloaded_episodes_count || 0;
      const total = show.total_episodes || 0;
      const { epLabel, airInfo, airClass, countdownAttr } = showAirSummary(show, statusKey);

      // The count is shown whenever something is downloaded. The bar also covers a show
      // whose total is unknown, filling against the episodes that are out (progressFraction).
      const showProgress = downloaded > 0 || total > 0;
      const showBar = total > 0 || downloaded > 0 || !!show.next_airing_episode;
      const pct = showBar ? Math.round(progressFraction(show) * 100) : 0;
      const progressText = `${downloaded}/${total || '?'}`;

      const name = escapeHtml(show.display_name);
      const isDimmed = isPaused || isCompleted;
      const dimClass = isDimmed ? 'opacity-80 grayscale-[35%]' : '';
      const noArt = `<div class="no-art ${dimClass}">${escapeHtml(showInitials(show.display_name))}</div>`;
      const art = show.cover_image
        ? `${noArt}<img src="${escapeHtml(show.cover_image)}" alt="${name}" class="absolute inset-0 w-full h-full object-cover ${dimClass}" loading="lazy" onerror="this.onerror=null;this.style.display='none'">`
        : noArt;
      const posterImg = `<div class="absolute inset-0 overflow-hidden"><div class="poster-zoom absolute inset-0">${art}</div></div>`;

      const statusChip = showStatusChip(label, cfg);

      // The bar sits flush under the poster, in the user's accent (grey while paused or finished).
      const barClass = isDimmed ? 'bg-zinc-500' : 'bg-accent';
      const bar = `<div class="h-[5px] bg-line-soft" title="${progressText} downloaded"><div class="h-full ${barClass}" style="width:${pct}%"></div></div>`;
      const airLine = (epLabel || airInfo || showProgress) ? `
              <div class="card-meta">
                <div class="card-meta-air">
                  <span class="font-medium text-zinc-300">${epLabel}</span>
                  <span class="show-countdown font-medium tabular-nums ${airClass}" ${countdownAttr} title="${localDateTime(show.next_airing_at)}">${airInfo}</span>
                </div>
                ${showProgress ? `<span class="flex-shrink-0 tabular-nums text-zinc-400" title="${progressText} downloaded">${progressText}</span>` : ''}
              </div>` : '';

      const pauseIcon = pauseIconSvg(isPaused);

      const problems = showProblemParts(show);
      const problemLine = problems.length ? `<div class="card-problems">${problems.join('')}</div>` : '';

      const feedName = show.current_feed_name ? escapeHtml(show.current_feed_name) : 'No feed';
      const feedLine = `
            <div class="card-meta justify-start" title="${feedName}">
              <svg class="w-3 h-3 flex-shrink-0 self-center" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M6 5c7.18 0 13 5.82 13 13M6 11a7 7 0 017 7m-6 0a1 1 0 11-2 0 1 1 0 012 0z"/></svg>
              <span class="truncate">${feedName}</span>
            </div>`;
      return `
        <div onclick="openShowPage(${show.id})" onpointerenter="prefetchShowPage(${show.id})" class="show-card flex flex-col group cursor-pointer">

          <div class="relative rounded-lg overflow-hidden border ${isDimmed ? 'border-line-soft' : 'border-line'} bg-zinc-900 group-hover:border-line-hover transition-colors duration-150">
            <div data-poster-id="${show.id}" class="on-art relative w-full aspect-[2/3] bg-zinc-900 overflow-hidden">
              ${posterImg}

              <div class="absolute top-2 right-2 z-20">${statusChip}</div>

              <div class="card-actions absolute top-2 left-2 flex items-center gap-1.5 z-20">
                ${!isCompleted ? `
                <button onclick="event.stopPropagation(); togglePauseShow(${show.id})" class="w-7 h-7 rounded-md flex items-center justify-center bg-black/70 hover:bg-black text-zinc-100 transition-colors active:scale-90" title="${isPaused ? 'Resume monitoring' : 'Pause monitoring'}">
                  ${pauseIcon}
                </button>
                ` : ''}
                <button onclick="event.stopPropagation(); deleteShow(${show.id})" class="w-7 h-7 rounded-md flex items-center justify-center bg-black/70 hover:bg-rose-600 text-zinc-100 transition-colors active:scale-90" title="Remove show">
                  <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2.2"><path stroke-linecap="round" stroke-linejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/></svg>
                </button>
              </div>
            </div>

            ${bar}
          </div>

          <div class="pt-2.5 px-0.5 flex flex-col gap-0.5">
            <h3 class="text-sm font-medium text-zinc-200 leading-[1.4] truncate" title="${name}">${name}</h3>${airLine}${problemLine}${feedLine}
          </div>

        </div>
      `;
    }

    // 17:00 or 5:00 PM, whichever the browser's locale uses.
    function formatTime(date) {
      if (!date) return '';
      return date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    }

    function renderCalendar() {
      const now = new Date();
      const currentDayOfWeek = (now.getDay() + 6) % 7;
      const nowMinutes = now.getHours() * 60 + now.getMinutes();

      const monday = new Date(now.getFullYear(), now.getMonth(), now.getDate());
      monday.setDate(monday.getDate() - currentDayOfWeek);

      const sunday = new Date(monday);
      sunday.setDate(monday.getDate() + 6);
      sunday.setHours(23, 59, 59, 999);

      const weekRangeEl = document.getElementById('calendar-week-range');
      if (weekRangeEl) {
        const startStr = monday.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
        const endStr = sunday.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
        weekRangeEl.textContent = `${startStr} – ${endStr}`;
      }

      const gridContainer = document.getElementById('calendar-weekly-grid');
      if (!gridContainer) return;

      let totalCalendarShows = 0;
      let gridHtml = '';

      for (let d = 0; d < 7; d++) {
        const colDate = new Date(monday);
        colDate.setDate(monday.getDate() + d);

        const isToday = (d === currentDayOfWeek);
        const isPastDay = (d < currentDayOfWeek);
        const dayOfWeek = colDate.getDay();
        const dayName = colDate.toLocaleDateString('en-US', { weekday: 'short' });
        const dateStr = colDate.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });

        const colShows = [];

        allShows.forEach(show => {
          if (!show.next_airing_at) return;
          const baseAirDate = new Date(show.next_airing_at);
          if (isNaN(baseAirDate.getTime())) return;

          if (show.is_released) {
            if (baseAirDate.getDay() === dayOfWeek) {
              const showInstanceDate = new Date(colDate);
              showInstanceDate.setHours(baseAirDate.getHours(), baseAirDate.getMinutes(), 0, 0);

              const timeMinutes = baseAirDate.getHours() * 60 + baseAirDate.getMinutes();
              const hasPassed = isPastDay || (isToday && (timeMinutes <= nowMinutes));
              const timeStr = formatTime(showInstanceDate);
              const isPaused = (show.status || '').toUpperCase() === 'PAUSED';

              colShows.push({
                show,
                instanceDate: showInstanceDate,
                timeMinutes,
                timeStr,
                hasPassed,
                isPaused,
              });
            }
          } else {
            if (baseAirDate >= monday && baseAirDate <= sunday && baseAirDate.toDateString() === colDate.toDateString()) {
              const timeMinutes = baseAirDate.getHours() * 60 + baseAirDate.getMinutes();
              const hasPassed = isPastDay || (isToday && (timeMinutes <= nowMinutes));
              const timeStr = formatTime(baseAirDate);
              const isPaused = (show.status || '').toUpperCase() === 'PAUSED';

              colShows.push({
                show,
                instanceDate: baseAirDate,
                timeMinutes,
                timeStr,
                hasPassed,
                isPaused,
              });
            }
          }
        });

        totalCalendarShows += colShows.length;

        colShows.sort((a, b) => a.timeMinutes - b.timeMinutes);

        const headerHtml = `
          <div class="pb-2 mb-2 border-b ${isToday ? 'border-accent/60' : isPastDay ? 'border-line-soft' : 'border-line'}">
            <div class="flex items-baseline gap-1.5 ${isPastDay ? 'opacity-80' : 'opacity-100'}">
              <span class="section-title ${isToday ? 'text-accent' : isPastDay ? 'text-zinc-400' : 'text-zinc-100'}">${dayName}</span>
              <span class="text-xs ${isPastDay ? 'text-zinc-500' : 'text-zinc-400'} font-medium">${dateStr}</span>
            </div>
          </div>
        `;

        const nowLine = '<div class="cal-now select-none">Now</div>';
        let itemsHtml = '';

        if (colShows.length === 0) {
          if (isToday) itemsHtml = nowLine;
        } else {
          const timeSlots = [];
          colShows.forEach(item => {
            const lastSlot = timeSlots[timeSlots.length - 1];
            if (lastSlot && lastSlot.timeMinutes === item.timeMinutes) {
              lastSlot.items.push(item);
            } else {
              timeSlots.push({
                timeMinutes: item.timeMinutes,
                timeStr: item.timeStr,
                hasPassed: item.hasPassed,
                items: [item],
              });
            }
          });

          let renderedLine = false;

          timeSlots.forEach((slot) => {
            if (isToday && !renderedLine && !slot.hasPassed) {
              itemsHtml += nowLine;
              renderedLine = true;
            }

            itemsHtml += createTimeSlotHtml(slot);
          });

          if (isToday && !renderedLine) itemsHtml += nowLine;
        }

        gridHtml += `
          <div class="cal-day ${isPastDay ? 'opacity-85' : 'opacity-100'}">
            ${headerHtml}
            <div class="cal-rail ${isToday ? 'cal-rail-today' : ''}">${itemsHtml}</div>
          </div>
        `;
      }

      gridContainer.innerHTML = gridHtml;

      const badgeEl = document.getElementById('badge-calendar-shows');
      if (badgeEl) {
        badgeEl.textContent = totalCalendarShows;
      }
    }

    function createTimeSlotHtml(slot) {
      const rows = slot.items.map(item => {
        const show = item.show;
        const { cfg, label } = resolveShowStatus(show);
        const dimmed = item.isPaused || item.hasPassed;
        const name = escapeHtml(show.display_name);
        const titleColor = (dimmed ? 'text-zinc-400' : 'text-zinc-100') + ' group-hover:text-accent-soft transition-colors';

        const poster = show.cover_image
          ? `<img src="${escapeHtml(show.cover_image)}" alt="${name}" class="w-16 aspect-[2/3] rounded-md object-cover flex-shrink-0 bg-canvas ${item.isPaused ? 'grayscale' : ''}" loading="lazy" onerror="this.onerror=null;this.style.display='none'">`
          : `<div class="relative w-16 aspect-[2/3] rounded-md overflow-hidden flex-shrink-0"><div class="no-art">${escapeHtml(showInitials(show.display_name))}</div></div>`;

        const epText = show.next_airing_episode ? `Ep ${show.next_airing_episode}` : (show.last_confirmed_episode ? `Ep ${show.last_confirmed_episode}` : 'Ep 1');

        return `
          <button type="button" onclick="openShowPage(${show.id})" onpointerenter="prefetchShowPage(${show.id})" class="group flex w-full gap-3 items-start text-left transition-opacity ${item.isPaused ? 'grayscale opacity-50 hover:opacity-80' : (item.hasPassed ? 'opacity-80 hover:opacity-100' : 'opacity-100 hover:opacity-90')}">
            ${poster}
            <div class="flex-1 min-w-0 pt-0.5">
              <h4 class="text-sm font-medium leading-snug line-clamp-3 ${titleColor}" title="${name}">${name}</h4>
              <div class="flex flex-wrap items-center gap-x-3 gap-y-1.5 mt-1">
                <span class="text-xs tabular-nums font-medium ${dimmed ? 'text-zinc-500' : 'text-zinc-400'}">${epText}</span>${showStatusChip(label, cfg)}
              </div>
            </div>
          </button>`;
      }).join('');

      return `
        <div class="cal-slot select-none">
          <div class="cal-time ${slot.hasPassed ? 'text-zinc-400' : 'text-zinc-100'}">${slot.timeStr}</div>
          <div class="space-y-3">${rows}</div>
        </div>
      `;
    }

    // Opening a show swaps the scroll container for #tab-show. Edits raise the save
    // pill; nothing is saved on leave, and leaving with unsaved edits asks first.

    let showListTab = 'episodes';
    let showSideTab = 'feed';
    let showReturnTab = 'shows';
    let showReturnScroll = 0;
    let showPagePushed = false;

    function setShowListTab(tab) {
      showListTab = tab;
      // Without a releases panel (rules mode before a pattern is learned) the episodes
      // stay shown; the next redraw opens the releases if it brings them.
      if (tab === 'feed' && !document.getElementById('show-panel-feed')) return;
      ['episodes', 'feed'].forEach(name => {
        const panel = document.getElementById(`show-panel-${name}`);
        const button = document.getElementById(`show-tab-${name}`);
        const count = document.getElementById(`show-count-${name}`);
        if (panel) panel.classList.toggle('hidden', name !== tab);
        if (count) count.classList.toggle('hidden', name !== tab);
        if (button) button.className = name === tab ? SEG_ACTIVE_CLASS : SEG_INACTIVE_CLASS;
      });
    }

    function setShowSideTab(tab) {
      showSideTab = tab;
      ['feed', 'download'].forEach(name => {
        const panel = document.getElementById(`show-side-${name}`);
        const button = document.getElementById(`show-side-tab-${name}`);
        if (panel) panel.classList.toggle('hidden', name !== tab);
        if (button) button.className = name === tab ? SEG_ACTIVE_CLASS : SEG_INACTIVE_CLASS;
      });
      const reset = document.getElementById('show-side-reset');
      if (reset) reset.classList.toggle('hidden', tab !== 'feed');
    }

    const DOWNLOAD_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" class="w-3.5 h-3.5"><path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/></svg>';

    const SWAP_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" class="w-3.5 h-3.5"><path d="M17 3l4 4-4 4"/><path d="M3 7h18"/><path d="M7 21l-4-4 4-4"/><path d="M21 17H3"/></svg>';

    const SPINNER_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" class="w-3.5 h-3.5 animate-spin"><path d="M12 3a9 9 0 1 0 9 9"/></svg>';

    // Downloads whose request is still running, so a redraw keeps their button disabled.
    const pendingDownloads = new Set();
    const pendingKey = (showId, title) => `${showId}|${title}`;

    // Rows carry a key and a signature of what they draw, so a redraw can keep the
    // elements that did not change instead of replacing them under the pointer.
    function rowAttrs(key, sig) {
      return `data-key="${escapeHtml(key)}" data-sig="${escapeHtml(String(sig))}"`;
    }

    function downloadButton(showId, title, tip, icon = DOWNLOAD_ICON) {
      const pending = pendingDownloads.has(pendingKey(showId, title));
      return `<button type="button" data-title="${escapeHtml(title)}" onclick="quickDownloadMatch(${showId}, this)" ${pending ? 'disabled' : ''}
        class="shrink-0 w-7 h-7 rounded-md flex items-center justify-center bg-accent/10 hover:bg-accent/25 border border-accent/30 hover:border-accent/60 text-accent-soft transition-colors active:scale-95 ${pending ? 'opacity-50 pointer-events-none' : ''}"
        title="${escapeHtml(tip)}">${pending ? SPINNER_ICON : icon}</button>`;
    }

    function replaceTip(episode, fromFeed) {
      return `Replace Ep ${episode} with this release. The current copy${fromFeed ? ` from ${fromFeed}` : ''} is deleted with its files once this one is seeding.`;
    }

    function restoreButton(showId, ep) {
      const pending = pendingDownloads.has(pendingKey(showId, `restore:${ep.id}`));
      return `<button type="button" onclick="restoreEpisode(${showId}, ${ep.id}, this)" ${pending ? 'disabled' : ''}
        class="shrink-0 w-7 h-7 rounded-md flex items-center justify-center bg-accent/10 hover:bg-accent/25 border border-accent/30 hover:border-accent/60 text-accent-soft transition-colors active:scale-95 ${pending ? 'opacity-50 pointer-events-none' : ''}"
        title="Restore: find this torrent in qBittorrent and manage it, or add it again from the feed.">${pending ? SPINNER_ICON : DOWNLOAD_ICON}</button>`;
    }

    function listShell(rows, emptyText) {
      return `<ul class="bg-canvas border border-line-soft rounded-lg px-3 flex-1 min-h-0 overflow-y-auto max-h-[60vh] lg:max-h-none divide-y divide-line-soft">${rows || `<li class="text-xs text-zinc-600 py-3 text-center">${emptyText}</li>`}</ul>`;
    }

    // "airs Sat 5:00 PM" for an episode still to come, with the date once it is a week or more away.
    function episodeAirLabel(ep) {
      if (String(ep.status).toLowerCase() !== 'wanted' || !ep.air_at) return '';
      const at = new Date(ep.air_at);
      const ahead = at - Date.now();
      if (isNaN(ahead) || ahead <= 0) return '';
      const when = ahead >= 6 * 86400000
        ? at.toLocaleDateString([], { month: 'short', day: 'numeric' })
        : at.toLocaleString([], { weekday: 'short', hour: 'numeric', minute: '2-digit' });
      return `airs ${when}`;
    }

    // `feed` is null while the feed lookup is still loading.
    function buildShowLists(showId, data, episodes, feed) {
      const isDirect = data.download_mode === 'direct';
      const loadingText = 'Checking the feed…';

      const replacements = new Map();
      const groups = (feed && feed.feed_groups) || [];
      groups.filter(g => g.usable).forEach(g => g.matches.forEach(m => {
        const best = replacements.get(m.episode);
        if (m.replaces && (!best || m.version > best.version)) replacements.set(m.episode, m);
      }));

      const episodeEntries = episodes.map(ep => {
        const done = String(ep.status).toLowerCase() === 'completed';
        const missed = String(ep.status).toLowerCase() === 'missed';
        // A wanted episode whose air time has passed is being searched for, unlike one still to come.
        const failed = String(ep.status).toLowerCase() === 'failed';
        const aired = String(ep.status).toLowerCase() === 'wanted' && ep.air_at && new Date(ep.air_at) <= Date.now();
        // A grab that could not be added is tried again later; the row says when.
        const retryAt = ep.retry_after ? new Date(ep.retry_after) : null;
        const retrying = aired && retryAt && retryAt > Date.now() && ep.last_error;
        const showPaused = data.status === 'paused';
        const statusLabel = retrying
          ? `retry ${retryAt.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`
          : aired ? (showPaused ? 'aired · paused' : 'aired · searching')
          : done && ep.downloaded_at ? formatRelativeTime(ep.downloaded_at)
          : episodeAirLabel(ep) || ep.status;
        const statusCls = done ? 'text-emerald-400' : failed ? 'text-rose-400' : missed || retrying ? 'text-amber-400' : aired && !showPaused ? 'text-sky-400' : 'text-zinc-500';
        const reason = ep.last_error ? ` title="${escapeHtml(ep.last_error)}"` : '';
        const retryMatch = failed && isDirect
          ? groups.filter(g => g.usable).flatMap(g => g.matches).find(m => m.episode === ep.episode_number && m.downloadable)
          : null;
        const unmanaged = isDirect && done && !ep.torrent_hash;
        const restoring = unmanaged && pendingDownloads.has(pendingKey(showId, `restore:${ep.id}`));
        const swap = isDirect && done && ep.torrent_hash ? replacements.get(ep.episode_number) : null;
        const swapPending = swap && pendingDownloads.has(pendingKey(showId, swap.title));
        const group = done ? 'done' : failed || missed || retrying ? 'attention' : ['downloading', 'queued', 'replacing'].includes(String(ep.status).toLowerCase()) ? 'active' : 'waiting';
        const html = `
          <li ${rowAttrs(`e:${ep.id}`, [ep.episode_number, ep.version, ep.release_title, statusLabel, ep.torrent_hash ? 1 : 0, restoring ? 1 : 0, swap ? swap.title : '', swapPending ? 1 : 0, ep.last_error || '', retryMatch ? retryMatch.title : ''].join('|'))} class="flex items-center gap-3 py-1.5 text-xs">
            <span class="w-20 shrink-0 text-accent-soft tabular-nums">Ep ${ep.episode_number}${ep.version > 1 ? ` · v${ep.version}` : ''}</span>
            <span class="flex-1 min-w-0 truncate text-zinc-500 font-mono" title="${escapeHtml(ep.release_title || '')}">${escapeHtml(ep.release_title || '')}</span>
            <span class="shrink-0 h-7 flex items-center justify-end gap-2">
              <span class="${statusCls}"${reason}>${escapeHtml(statusLabel)}${unmanaged ? ' · unmanaged' : ''}</span>
              ${retryMatch ? downloadButton(showId, retryMatch.title, 'Try this release again', DOWNLOAD_ICON) : ''}
              ${unmanaged ? restoreButton(showId, ep) : ''}
              ${swap ? downloadButton(showId, swap.title, replaceTip(ep.episode_number, ep.feed_name), SWAP_ICON) : ''}
            </span>
          </li>`;
        return { ep, group, html };
      });

      // Newest episode first inside each group; a group with no episodes is left out.
      const EPISODE_GROUPS = [['attention', 'Needs attention'], ['active', 'Downloading'], ['done', 'Completed'], ['waiting', 'Waiting']];
      const episodeRows = EPISODE_GROUPS.map(([name, label]) => {
        const entries = episodeEntries.filter(entry => entry.group === name).sort((a, b) => b.ep.episode_number - a.ep.episode_number);
        if (!entries.length) return '';
        const hasMissed = entries.some(entry => String(entry.ep.status).toLowerCase() === 'missed');
        const searching = actionsInFlight.has(`search-releases-${showId}`);
        const search = !(isDirect && hasMissed) ? ''
          : searching ? `<span class="shrink-0 inline-flex items-center gap-1.5 text-zinc-400">${SPINNER_ICON}Searching…</span>`
          : `<button type="button" onclick="searchPastReleases()" class="shrink-0 text-zinc-400 hover:text-zinc-100 underline underline-offset-4 decoration-accent/50 hover:decoration-accent transition-colors" title="Ask the feeds' sites for the episodes that are no longer in their RSS">Search older</button>`;
        const header = `
          <li ${rowAttrs(`eg:${name}`, [label, entries.length, search ? (searching ? 2 : 1) : 0].join('|'))} class="flex items-center gap-2 py-2 text-xs sticky top-0 z-10" style="background-color: rgb(var(--field))">
            <span class="font-medium text-zinc-200">${label}</span>
            <span class="shrink-0 text-zinc-500 tabular-nums">${entries.length}</span>
            <span class="ml-auto">${search}</span>
          </li>`;
        return header + entries.map(entry => entry.html).join('');
      }).join('');

      // Releases are what the feeds carry for this show. Direct mode lists them by feed;
      // a rule only has them once it has learned a pattern.
      let releaseRows = '';
      let releaseCount = 'checking feed…';
      let releaseEmpty = loadingText;
      let hasReleases = false;
      if (isDirect) {
        hasReleases = true;
        const matches = groups.filter(g => g.usable).flatMap(g => g.matches);
        const ROLE_LABELS = { locked: 'Locked', picked: 'Picked', assigned: 'Assigned', other: 'Not used by this show' };
        const groupHeader = g => `
          <li ${rowAttrs(`h:${g.feed_id}`, [g.feed_name, g.role, g.matches.length].join('|'))} class="flex items-center gap-2 py-2 text-xs sticky top-0 z-10" style="background-color: rgb(var(--field))">
            <span class="font-medium text-zinc-200 truncate">${escapeHtml(g.feed_name)}</span>
            ${ROLE_LABELS[g.role] ? `<span class="shrink-0 text-zinc-500">· ${ROLE_LABELS[g.role]}</span>` : ''}
            <span class="shrink-0 ml-auto text-zinc-500 tabular-nums">${g.matches.length}</span>
            ${g.role === 'other' ? `<button type="button" onclick="useFeedFromReleases(${g.feed_id})" class="shrink-0 text-zinc-400 hover:text-zinc-100 underline underline-offset-4 decoration-accent/50 hover:decoration-accent transition-colors" title="Move this show to ${escapeHtml(g.feed_name)}">Use this feed</button>` : ''}
          </li>`;
        const matchRow = (g, m) => `
          <li ${rowAttrs(`f:${g.feed_id}:${m.title}`, [m.episode, m.version, m.downloadable ? 1 : 0, m.restore ? 1 : 0, m.replaces ? 1 : 0, m.episode_status, pendingDownloads.has(pendingKey(showId, m.title)) ? 1 : 0].join('|'))} class="flex items-center gap-3 py-1.5 text-xs">
            <span class="w-20 shrink-0 text-accent-soft tabular-nums">Ep ${m.episode}${m.version > 1 ? ` · v${m.version}` : ''}</span>
            <span class="flex-1 min-w-0 truncate text-zinc-400 font-mono select-all" title="${escapeHtml(m.display_title && m.display_title !== m.title ? `${m.display_title}
${m.title}` : m.title)}">${escapeHtml(m.display_title || m.title)}</span>
            <span class="shrink-0 w-24 h-7 flex items-center justify-end">
              ${m.downloadable
                ? (m.replaces
                  ? downloadButton(showId, m.title, replaceTip(m.episode, ''), SWAP_ICON)
                  : downloadButton(showId, m.title, m.restore
                    ? 'Restore: add this release to qBittorrent, or tag the copy already there, so Kisetsu manages it.'
                    : 'Download this release now. It is tracked like any other episode.'))
                : `<span class="text-zinc-600">${escapeHtml(m.episode_status)}</span>`}
            </span>
          </li>`;
        releaseRows = groups.map(g => groupHeader(g) + g.matches.map(m => matchRow(g, m)).join('')).join('');
        if (feed) releaseCount = `${matches.length} release${matches.length === 1 ? '' : 's'}`;
        releaseEmpty = feed ? 'Nothing in any feed matches this show right now. Try Search older.' : loadingText;
      } else if (data.has_qbit_rule && data.has_learned_pattern) {
        hasReleases = true;
        const articles = feed ? (feed.matched_articles || []) : [];
        releaseRows = articles.map(a => `
          <li ${rowAttrs(`r:${a}`, pendingDownloads.has(pendingKey(showId, a)) ? 1 : 0)} class="flex items-center gap-3 py-1.5 text-xs">
            <span class="flex-1 min-w-0 truncate text-zinc-400 font-mono select-all" title="${escapeHtml(a)}">${escapeHtml(a)}</span>
            ${downloadButton(showId, a, 'Download this release now with the same save path, category and ratio as the rule')}
          </li>`).join('');
        if (feed) releaseCount = `${articles.length} release${articles.length === 1 ? '' : 's'}`;
        releaseEmpty = feed ? 'No cached RSS articles currently match this rule pattern.' : loadingText;
      }

      const tab = hasReleases && showListTab === 'feed' ? 'feed' : 'episodes';
      const searching = actionsInFlight.has(`search-releases-${showId}`);
      const searchControl = searching
        ? `<span class="inline-flex items-center gap-1.5 text-xs text-zinc-400">${SPINNER_ICON}Searching…</span>`
        : `<button type="button" onclick="searchPastReleases()" class="text-xs text-zinc-400 hover:text-zinc-100 underline underline-offset-4 decoration-accent/50 hover:decoration-accent transition-colors" title="Ask the feeds' sites for this show's older releases, for episodes that are no longer in their RSS">Search older</button>`;
      const heading = hasReleases
        ? `<div class="seg">
            <button type="button" id="show-tab-episodes" onclick="setShowListTab('episodes')" class="${tab === 'episodes' ? SEG_ACTIVE_CLASS : SEG_INACTIVE_CLASS}">Episodes</button>
            <button type="button" id="show-tab-feed" onclick="setShowListTab('feed')" class="${tab === 'feed' ? SEG_ACTIVE_CLASS : SEG_INACTIVE_CLASS}">Releases</button>
          </div>`
        : '<h3 class="section-title">Episodes</h3>';
      return `
        <div class="pane-head">
          ${heading}
          <span class="flex items-center gap-3">
            <span id="show-count-episodes" class="text-xs text-zinc-500 tabular-nums ${tab === 'episodes' ? '' : 'hidden'}">${episodes.length} tracked</span>
            ${hasReleases ? `<span id="show-count-feed" class="text-xs text-zinc-500 tabular-nums ${tab === 'feed' ? '' : 'hidden'}">${releaseCount}</span>` : ''}
            ${searchControl}
          </span>
        </div>
        <div class="card p-4 flex-1 min-h-0 flex flex-col">
          <div id="show-panel-episodes" class="flex-1 min-h-0 flex flex-col ${tab === 'episodes' ? '' : 'hidden'}">${listShell(episodeRows, 'No episode records yet.')}</div>
          ${hasReleases ? `<div id="show-panel-feed" class="flex-1 min-h-0 flex flex-col ${tab === 'feed' ? '' : 'hidden'}">${listShell(releaseRows, releaseEmpty)}</div>` : ''}
        </div>`;
    }

    function showListsMarkup(showId, data, episodes, feed) {
      return buildShowLists(showId, data, episodes, feed);
    }

    // What the open page was drawn from, so the lists can be redrawn when the
    // slower feed lookup lands or after a download.
    let showContext = null;
    let showFeedState = null;
    let showInitialState = null;

    // What #show-lists currently shows, so a redraw with identical content can be
    // skipped instead of replacing buttons under the pointer.
    let drawnListsHtml = '';

    function setShowListsHtml(html) {
      drawnListsHtml = html;
      document.getElementById('show-lists').innerHTML = html;
    }

    function rerenderShowLists() {
      const target = document.getElementById('show-lists');
      if (!target || !showContext || currentInspectedShowId !== showContext.showId) return;
      const html = showListsMarkup(showContext.showId, showContext.data, showContext.episodes, showFeedState);
      if (html === drawnListsHtml) return;
      syncShowLists(target, html);
      drawnListsHtml = html;
    }

    // Make a list's rows match the new ones, keeping the existing element for every
    // row whose signature is unchanged. Returns false when the rows carry no keys.
    function syncListRows(current, next) {
      const nextRows = Array.from(next.children);
      if (!nextRows.every(row => row.dataset.key)) return false;
      const currentByKey = new Map();
      Array.from(current.children).forEach(row => { if (row.dataset.key) currentByKey.set(row.dataset.key, row); });
      const wanted = nextRows.map(row => {
        const existing = currentByKey.get(row.dataset.key);
        return existing && existing.dataset.sig === row.dataset.sig ? existing : row;
      });
      wanted.forEach((row, index) => {
        if (current.children[index] !== row) current.insertBefore(row, current.children[index] || null);
      });
      while (current.children.length > wanted.length) current.lastElementChild.remove();
      return true;
    }

    // Redraw #show-lists without replacing the rows that did not change: a click
    // that starts on a button must still land on it when a refresh arrives
    // between the press and the release.
    function syncShowLists(target, html) {
      const next = document.createElement('div');
      next.innerHTML = html;
      const currentLists = Array.from(target.querySelectorAll('ul'));
      const nextLists = Array.from(next.querySelectorAll('ul'));
      const scrolls = currentLists.map(list => list.scrollTop);
      if (currentLists.length && currentLists.length === nextLists.length) {
        currentLists.forEach((list, index) => {
          if (syncListRows(list, nextLists[index])) nextLists[index].replaceWith(list);
        });
      }
      target.replaceChildren(...next.childNodes);
      target.querySelectorAll('ul').forEach((list, index) => { list.scrollTop = scrolls[index] || 0; });
    }

    async function loadShowFeedMatches(showId) {
      let result;
      try {
        result = await apiFetch(`/api/shows/${showId}/feed-matches`);
      } catch (err) {
        result = { matched_articles: [], feed_groups: [] };
      }
      if (currentInspectedShowId !== showId) return;
      showFeedState = result;
      rerenderShowLists();
    }

    async function refreshShowLists(showId) {
      if (!showContext || currentInspectedShowId !== showId) return;
      try {
        const episodes = await apiFetch(`/api/shows/${showId}/episodes`);
        if (currentInspectedShowId !== showId) return;
        showContext.episodes = Array.isArray(episodes) ? episodes : [];
        rerenderShowLists();
      } catch (err) {
        // The lists are informational; the toast from the download already reported the outcome.
      }
      await loadShowFeedMatches(showId);
      scheduleShowPoll(showId);
    }

    // Episodes in flight change on the server without any click, so while one is
    // queued, downloading or replacing the episode list is re-read every few
    // seconds. When the last one settles the feed list is refreshed once too.
    const IN_FLIGHT_EPISODE_STATUSES = new Set(['queued', 'downloading', 'replacing']);
    const SHOW_POLL_MS = 4000;
    let showPollTimer = null;
    let showPollActive = false;

    function scheduleShowPoll(showId) {
      clearTimeout(showPollTimer);
      showPollTimer = null;
      if (!showContext || currentInspectedShowId !== showId) { showPollActive = false; return; }
      const inFlight = showContext.episodes.some(ep => IN_FLIGHT_EPISODE_STATUSES.has(String(ep.status).toLowerCase()));
      if (!inFlight) {
        if (showPollActive) {
          showPollActive = false;
          loadShowFeedMatches(showId);
        }
        return;
      }
      showPollActive = true;
      showPollTimer = setTimeout(async () => {
        if (currentInspectedShowId !== showId) { showPollActive = false; return; }
        try {
          const episodes = await apiFetch(`/api/shows/${showId}/episodes`);
          if (currentInspectedShowId === showId && showContext) {
            showContext.episodes = Array.isArray(episodes) ? episodes : [];
            rerenderShowLists();
          }
        } catch (err) {
          // Try again on the next tick.
        }
        scheduleShowPoll(showId);
      }, SHOW_POLL_MS);
    }

    // Started when the pointer enters a card, so the data is usually there by the
    // time the click lands. An entry is used once, then dropped, so reopening a
    // show after an edit never shows stale values.
    const showPrefetch = new Map();
    const PREFETCH_MAX_AGE_MS = 10000;

    function startShowFetch(showId) {
      const entry = {
        at: Date.now(),
        rule: apiFetch(`/api/shows/${showId}/rule`),
        episodes: apiFetch(`/api/shows/${showId}/episodes`).catch(() => []),
      };
      entry.rule.catch(() => {});
      // Once both have arrived the page can be drawn without waiting.
      Promise.all([entry.rule, entry.episodes]).then(([data, episodes]) => {
        entry.data = data;
        entry.episodeList = Array.isArray(episodes) ? episodes : [];
        entry.ready = true;
      }).catch(() => {});
      return entry;
    }

    function prefetchShowPage(showId) {
      const existing = showPrefetch.get(showId);
      if (existing && Date.now() - existing.at < PREFETCH_MAX_AGE_MS) return;
      showPrefetch.set(showId, startShowFetch(showId));
    }

    function takeShowFetch(showId) {
      const entry = showPrefetch.get(showId);
      showPrefetch.delete(showId);
      if (entry && Date.now() - entry.at < PREFETCH_MAX_AGE_MS) return entry;
      return startShowFetch(showId);
    }

    function showBodySkeleton() {
      const block = '<div class="h-4 mb-2.5"></div><div class="flex-1 min-h-[12rem] rounded-xl bg-raised/60"></div>';
      document.getElementById('show-side-form').innerHTML = block;
      setShowListsHtml(block);
    }

    // A paused show keeps the colour of what it was before, and an unconfirmed one
    // reads as Upcoming until it has aired.
    function resolveShowStatus(show) {
      const rawStatus = (show.status || 'UNCONFIRMED').toUpperCase();
      const isPaused = rawStatus === 'PAUSED';
      const statusKey = isPaused ? ((show.status_before_pause || 'UNCONFIRMED').toUpperCase()) : rawStatus;

      let cfg = STATUS_CONFIG[statusKey] || STATUS_CONFIG['UNCONFIRMED'];
      let label = cfg.label;

      if (!isPaused && statusKey === 'UNCONFIRMED') {
        if (!show.is_released) {
          label = 'Upcoming';
          cfg = STATUS_CONFIG['UPCOMING'];
        } else if (show.waiting_for_release) {
          label = 'Waiting';
          cfg = STATUS_CONFIG['WAITING'];
        } else {
          label = 'Testing';
          cfg = STATUS_CONFIG['UNCONFIRMED'];
        }
      } else if (isPaused) {
        label = 'Paused';
        cfg = STATUS_CONFIG['PAUSED'];
      }
      return { isPaused, isCompleted: isShowCompleted(show), statusKey, cfg, label };
    }

    // "Ep n" plus a countdown or outcome, shared by the cards and the page header.
    function showAirSummary(show, statusKey) {
      const total = show.total_episodes || 0;
      let epLabel = '';
      let airInfo = '';
      let airClass = 'text-zinc-100';
      let countdownAttr = '';
      if (statusKey === 'COMPLETED') {
        epLabel = total ? `${total} eps` : '';
        airInfo = 'Completed';
        airClass = 'text-zinc-300';
      } else if (show.next_airing_episode && show.next_airing_at) {
        const cd = formatEpisodeCountdown(show.next_airing_at);
        if (cd === 'Aired' && show.last_confirmed_episode && show.last_confirmed_episode >= show.next_airing_episode) {
          epLabel = `Ep ${show.last_confirmed_episode}`;
          airInfo = 'Downloaded';
          airClass = 'text-emerald-300';
        } else {
          epLabel = `Ep ${show.next_airing_episode}`;
          airInfo = formatCardCountdown(cd, localDateTime(show.next_airing_at));
          if (cd === 'Aired' && !show.waiting_for_release) airClass = 'text-amber-300';
          countdownAttr = `data-air-at="${show.next_airing_at}" data-date-str="${localDateTime(show.next_airing_at)}"`;
        }
      } else if (show.last_confirmed_episode) {
        epLabel = `Ep ${show.last_confirmed_episode}`;
      } else if (statusKey === 'STALLED') {
        airInfo = 'Stalled';
        airClass = 'text-rose-300';
      }
      return { epLabel, airInfo, airClass, countdownAttr };
    }

    function showBannerMarkup(show) {
      const src = show.banner_image || show.cover_image;
      if (!src) return '';
      if (!show.banner_image) {
        return `<img src="${escapeHtml(src)}" alt="" class="blur-art blur-art-banner" onerror="this.onerror=null;this.style.display='none'">`;
      }
      return `<img src="${escapeHtml(src)}" alt="" class="absolute inset-0 w-full h-full object-cover" onerror="this.onerror=null;this.style.display='none'">`;
    }

    // Drawn from the list data the page already has, so it appears instantly; the
    // forms fill in when the detail request returns.
    function renderShowHeader(show) {
      const block = document.getElementById('show-title-block');
      const poster = document.getElementById('show-poster');
      const art = document.getElementById('show-banner-art');
      const backdrop = document.getElementById('show-backdrop-art');
      const titleEl = document.getElementById('page-title');
      if (!block || !poster || !art) return;

      if (!show) {
        block.dataset.sig = '';
        art.dataset.sig = '';
        art.innerHTML = '';
        backdrop.innerHTML = '';
        applyShowPalette(null);
        poster.innerHTML = '';
        block.innerHTML = '<div class="space-y-3"><div class="h-7 w-2/3 rounded-md bg-raised/60"></div><div class="h-4 w-1/3 rounded-md bg-raised/60"></div></div>';
        return;
      }

      const sig = JSON.stringify(show);
      if (block.dataset.sig === sig) return;
      block.dataset.sig = sig;

      const { isPaused, isCompleted, statusKey, cfg, label } = resolveShowStatus(show);
      const { epLabel, airInfo, airClass, countdownAttr } = showAirSummary(show, statusKey);
      const name = escapeHtml(show.display_name);
      if (titleEl) titleEl.textContent = show.display_name;

      const artSig = `${show.banner_image || ''}|${show.cover_image || ''}`;
      if (art.dataset.sig !== artSig) {
        art.dataset.sig = artSig;
        art.innerHTML = showBannerMarkup(show);
        const backdropSrc = show.banner_image || show.cover_image;
        backdrop.innerHTML = backdropSrc
          ? `<img src="${escapeHtml(backdropSrc)}" alt="" class="blur-art blur-art-backdrop" onerror="this.onerror=null;this.style.display='none'">`
          : '';
      }

      applyShowPalette(show.accent_hues);
      if (!show.accent_ready) fetchShowAccent(show.id);

      const dimClass = (isPaused || isCompleted) ? 'opacity-80 grayscale-[35%]' : '';
      poster.innerHTML = show.cover_image
        ? `<img src="${escapeHtml(show.cover_image)}" alt="${name}" class="absolute inset-0 w-full h-full object-cover ${dimClass}" onerror="this.onerror=null;this.style.display='none'">`
        : '<div class="absolute inset-0 flex items-center justify-center text-zinc-600 text-xs">No Art</div>';

      const alt = [show.title_romaji, show.title_english].find(t => t && t !== show.display_name);
      const season = show.season_name
        ? `${show.season_name.charAt(0)}${show.season_name.slice(1).toLowerCase()}${show.season_year ? ` ${show.season_year}` : ''}`
        : (show.season_year ? String(show.season_year) : '');
      const subtitle = [alt, season].filter(Boolean).map(escapeHtml).join(' · ');

      const downloaded = show.downloaded_episodes_count || 0;
      const total = show.total_episodes || 0;
      const showProgress = downloaded > 0 || total > 0 || !!show.next_airing_episode;
      const pct = Math.round(progressFraction(show) * 100);
      const barClass = (isPaused || isCompleted) ? 'bg-zinc-400' : 'bg-accent';

      const feedName = show.current_feed_name ? escapeHtml(show.current_feed_name) : 'No feed';
      const pauseIcon = isPaused
        ? '<svg class="w-3.5 h-3.5" fill="currentColor" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>'
        : '<svg class="w-3.5 h-3.5" fill="currentColor" viewBox="0 0 24 24"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>';

      block.innerHTML = `
        <div class="flex flex-wrap items-start gap-x-4 gap-y-3">
          <div class="min-w-0 flex-1 basis-64">
            <div class="flex items-center gap-2 min-w-0">
              <h2 class="show-title truncate" title="${name}">${name}</h2>
              <a href="https://anilist.co/anime/${show.anilist_id}" target="_blank" rel="noopener noreferrer" class="flex-shrink-0 text-accent-soft hover:text-zinc-50 transition-colors" title="Open on AniList" aria-label="Open on AniList">
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14"/></svg>
              </a>
            </div>
            ${subtitle ? `<p class="text-sm text-zinc-400 mt-1 truncate">${subtitle}</p>` : ''}
          </div>
          <div class="flex items-center gap-2 flex-shrink-0 ml-auto">
            ${!isCompleted ? `<button type="button" onclick="togglePauseShow(${show.id})" class="btn btn-sm">${pauseIcon}${isPaused ? 'Resume' : 'Pause'}</button>` : ''}
            <button type="button" onclick="deleteShow(${show.id})" class="btn btn-danger btn-sm">Delete</button>
          </div>
        </div>
        <div class="flex flex-wrap items-center gap-x-3 gap-y-1.5 text-xs mt-3">
          <span class="px-2 py-0.5 inline-flex items-center rounded font-medium border ${cfg.bg}">${label}</span>
          ${(epLabel || airInfo) ? `
          <span class="text-[13px]">
            <span class="font-semibold text-zinc-100">${epLabel}</span>
            <span class="show-countdown font-medium tabular-nums ml-1.5 ${airClass}" ${countdownAttr} title="${localDateTime(show.next_airing_at)}">${airInfo}</span>
          </span>` : ''}
          <span class="inline-flex items-center gap-1.5 text-zinc-500 min-w-0" title="${feedName}">
            <svg class="w-3 h-3 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M6 5c7.18 0 13 5.82 13 13M6 11a7 7 0 017 7m-6 0a1 1 0 11-2 0 1 1 0 012 0z"/></svg>
            <span class="truncate">${feedName}</span>
          </span>
        </div>
        ${showProgress ? `
        <div class="flex items-center gap-3 max-w-sm mt-3">
          <div class="flex-1 h-2 rounded-full bg-zinc-100/15 overflow-hidden"><div class="h-full ${barClass}" style="width:${pct}%"></div></div>
          <span class="text-xs tabular-nums text-zinc-400">${downloaded}/${total || '?'} downloaded</span>
        </div>` : ''}`;
    }

    // Called after every list refresh, so pause/resume and the countdown stay current
    // without touching the forms (which may hold unsaved edits).
    function syncShowPageHeader() {
      if (currentInspectedShowId === null) return;
      const show = allShows.find(s => s.id === currentInspectedShowId);
      if (!show) {
        // Deleted, or a stale #show/<id> link.
        showInitialState = null;
        clearShowHash();
        leaveShowPage();
        return;
      }
      renderShowHeader(show);
    }

    function showIdFromHash() {
      const m = /^#show[/]([0-9]+)$/.exec(location.hash);
      return m ? parseInt(m[1]) : null;
    }

    function clearShowHash() {
      if (showIdFromHash() !== null) history.replaceState(null, '', location.pathname + location.search);
    }

    // Per-show accent colours. The server reads them from the banner (or the poster
    // when there is no banner); they arrive as RGB triples set as CSS variables on
    // #tab-show, so everything inside it that uses the accent follows them.
    function hslToRgb(h, s, l) {
      const c = (1 - Math.abs(2 * l - 1)) * s;
      const x = c * (1 - Math.abs(((h / 60) % 2) - 1));
      const m = l - c / 2;
      const [r, g, b] = h < 60 ? [c, x, 0] : h < 120 ? [x, c, 0] : h < 180 ? [0, c, x] : h < 240 ? [0, x, c] : h < 300 ? [x, 0, c] : [c, 0, x];
      return [r + m, g + m, b + m].map(v => Math.round(v * 255));
    }

    function relativeLuminance([r, g, b]) {
      const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
      return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
    }

    // Light-mode accents are worked out in OKLCH. Darkening a colour in HSL drags its hue: a
    // violet at 40% lightness comes out blue. In OKLCH a colour keeps its hue as it darkens.
    function srgbToLinear(v) {
      v /= 255;
      return v <= 0.04045 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    }

    function rgbToOklch([r, g, b]) {
      const [lr, lg, lb] = [r, g, b].map(srgbToLinear);
      const l = Math.cbrt(0.4122214708 * lr + 0.5363325363 * lg + 0.0514459929 * lb);
      const m = Math.cbrt(0.2119034982 * lr + 0.6806995451 * lg + 0.1073969566 * lb);
      const s = Math.cbrt(0.0883024619 * lr + 0.2817188376 * lg + 0.6299787005 * lb);
      const L = 0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s;
      const a = 1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s;
      const bb = 0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s;
      return [L, Math.hypot(a, bb), (Math.atan2(bb, a) * 180 / Math.PI + 360) % 360];
    }

    function oklchToLinear(L, C, h) {
      const a = C * Math.cos(h * Math.PI / 180), b = C * Math.sin(h * Math.PI / 180);
      const l = Math.pow(L + 0.3963377774 * a + 0.2158037573 * b, 3);
      const m = Math.pow(L - 0.1055613458 * a - 0.0638541728 * b, 3);
      const s = Math.pow(L - 0.0894841775 * a - 1.2914855480 * b, 3);
      return [
        4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
        -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
        -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s,
      ];
    }

    function oklchToRgb(L, C, h) {
      return oklchToLinear(L, C, h).map(v => {
        const c = v <= 0.0031308 ? v * 12.92 : 1.055 * Math.pow(v, 1 / 2.4) - 0.055;
        return Math.round(Math.min(1, Math.max(0, c)) * 255);
      });
    }

    // The largest chroma up to C that stays inside sRGB. Pulling a colour towards grey keeps its
    // hue; clipping a channel would shift it.
    function fitChroma(L, C, h) {
      const inGamut = (c) => oklchToLinear(L, c, h).every(v => v >= -0.0005 && v <= 1.0005);
      if (inGamut(C)) return C;
      let lo = 0, hi = C;
      for (let i = 0; i < 12; i++) {
        const mid = (lo + hi) / 2;
        if (inGamut(mid)) lo = mid; else hi = mid;
      }
      return lo;
    }

    function hexToRgb(hex) {
      const n = parseInt(hex.slice(1), 16);
      return [n >> 16 & 255, n >> 8 & 255, n & 255];
    }

    // The accent roles on a light surface, for a colour with OKLCH hue `h` and chroma `C`, and
    // a secondary hue `h2`. Material 3 puts the light-mode primary at tone 40 (L* 40) with
    // white on it, which is about 0.47 in OKLCH; that also keeps accent text above 5:1 on
    // white. `ac` is the main accent (progress bars, links), `soft` the accent used for text
    // and `strong` the fill behind button text.
    function lightAccentRoles(h, C, h2) {
      const c = Math.min(0.2, C);
      const at = (hue, L, k = 1) => oklchToRgb(L, fitChroma(L, c * k, hue), hue);
      return {
        ac: at(h, 0.47).join(' '),
        soft: at(h, 0.42, 0.9).join(' '),
        strong: at(h, 0.47).join(' '),
        ink: '255 255 255',
        ac2: at(h2, 0.47).join(' '),
      };
    }

    // The same, from the HSL values the show palette carries (hue, saturation, secondary hue).
    // The hue is read from a pastel of the colour, which is where OKLCH and HSL agree best.
    function lightAccentRolesFromHsl(h1, s, h2) {
      const [, C, h] = rgbToOklch(hslToRgb(h1, s, 0.68));
      return lightAccentRoles(h, C, rgbToOklch(hslToRgb(h2, s, 0.68))[2]);
    }

    // `hues` is [primary hue, saturation, secondary hue].
    // How strongly the page surfaces take the hue (0 keeps them grey).
    const THEME_TINT = 0.6;

    // `hues` is [accent hue, accent saturation, secondary hue, tint hue, tint saturation].
    // `mode` is 'dark' or 'light'. Light keeps the tint weaker, since saturation reads
    // stronger on pale surfaces, and uses darker accents so accent text stays readable.
    function paletteFromHues([h1, s1, h2, th, ts], mode = 'dark') {
      const light = mode === 'light';
      const s = Math.min(0.85, Math.max(0.4, s1));
      const warm = th >= 10 && th <= 60 ? 0.7 : 1;
      const t = Math.min(0.5, ts) * THEME_TINT * warm * (light ? 0.9 : 1);
      const surface = (l, k = 1) => hslToRgb(th, t * k, l).join(' ');
      const strong = hslToRgb(h1, s, 0.56);
      const surfaces = light ? {
        bg: surface(0.925), sunken: surface(0.875), chrome: surface(0.9), surface: surface(0.972), field: surface(0.99),
        raised: surface(0.9), raised2: surface(0.86), line: surface(0.82, 0.9), lineSoft: surface(0.87, 0.9),
        lineHover: surface(0.76, 0.9), lineStrong: surface(0.64, 0.9),
      } : {
        bg: surface(0.085), sunken: surface(0.065), chrome: surface(0.10), surface: surface(0.115), field: surface(0.07),
        raised: surface(0.155), raised2: surface(0.19), line: surface(0.21, 0.9), lineSoft: surface(0.16, 0.9),
        lineHover: surface(0.26, 0.9), lineStrong: surface(0.32, 0.9),
      };
      if (light) return { ...surfaces, ...lightAccentRolesFromHsl(h1, s, h2) };
      return {
        ...surfaces,
        ac: hslToRgb(h1, s, 0.62).join(' '),
        soft: hslToRgb(h1, s, 0.76).join(' '),
        strong: strong.join(' '),
        ink: relativeLuminance(strong) < 0.2 ? '255 255 255' : '8 10 14',
        ac2: hslToRgb(h2, s, 0.62).join(' '),
      };
    }

    // The variables live on <html>, so the sidebar and header bar inherit them too.
    let currentShowHues = null;
    function applyShowPalette(hues) {
      currentShowHues = hues || null;
      const section = document.documentElement;
      const keys = {
        '--ac': 'ac', '--ac-soft': 'soft', '--ac-strong': 'strong', '--ac-ink': 'ink', '--ac2': 'ac2',
        '--bg': 'bg', '--sunken': 'sunken', '--chrome': 'chrome', '--surface': 'surface', '--field': 'field', '--raised': 'raised', '--raised2': 'raised2',
        '--line': 'line', '--line-soft': 'lineSoft', '--line-hover': 'lineHover', '--line-strong': 'lineStrong',
      };
      const palette = hues ? paletteFromHues(hues, resolvedTheme()) : null;
      Object.keys(keys).forEach(name => {
        if (palette) section.style.setProperty(name, palette[keys[name]]);
        else section.style.removeProperty(name);
      });
    }

    // Theme: 'dark' (default), 'light' or 'system'. Kept in this browser only; the head
    // script applies it before first paint.
    const THEME_KEY = 'kisetsu_theme';
    function themeMode() {
      try { return localStorage.getItem(THEME_KEY) || 'dark'; } catch (err) { return 'dark'; }
    }
    function resolvedTheme() {
      const mode = themeMode();
      return mode === 'light' || (mode === 'system' && window.matchMedia('(prefers-color-scheme: light)').matches) ? 'light' : 'dark';
    }
    function applyTheme() {
      document.documentElement.setAttribute('data-theme', resolvedTheme());
      applyShowPalette(currentShowHues);
      renderThemeControls();
    }
    function setThemeMode(mode) {
      try { localStorage.setItem(THEME_KEY, mode); } catch (err) {}
      applyTheme();
    }
    function toggleTheme() {
      setThemeMode(resolvedTheme() === 'light' ? 'dark' : 'light');
    }
    function renderThemeControls() {
      const mode = themeMode();
      ['system', 'light', 'dark'].forEach(name => {
        const button = document.getElementById(`btn-theme-${name}`);
        if (button) button.className = name === mode ? SEG_ACTIVE_CLASS : SEG_INACTIVE_CLASS;
      });
      const light = resolvedTheme() === 'light';
      const sun = document.getElementById('icon-theme-sun');
      const moon = document.getElementById('icon-theme-moon');
      if (sun) sun.classList.toggle('hidden', light);
      if (moon) moon.classList.toggle('hidden', !light);
      const toggle = document.getElementById('btn-theme');
      if (toggle) {
        const label = light ? 'Switch to dark theme' : 'Switch to light theme';
        toggle.title = label;
        toggle.setAttribute('aria-label', label);
      }
    }
    window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', () => {
      if (themeMode() === 'system') applyTheme();
    });

    const DEFAULT_ACCENT = '#2dd4bf';
    const ACCENT_PRESETS = [
      ['Teal', '#2dd4bf'], ['Sky', '#38bdf8'], ['Indigo', '#818cf8'], ['Violet', '#a78bfa'], ['Pink', '#f472b6'],
      ['Rose', '#fb7185'], ['Orange', '#fb923c'], ['Amber', '#fbbf24'], ['Lime', '#a3e635'], ['Emerald', '#34d399'],
    ];
    const ACCENT_TINTS = { off: 0, subtle: 0.22, full: 0.5 };
    const ACCENT_CSS_KEY = 'kisetsu_accent_css4';
    const STOCK_ACCENT = {
      dark: { ac: '45 212 191', soft: '94 234 212', strong: '20 184 166', ink: '4 32 30', ac2: '56 189 248' },
      light: { ac: '13 148 136', soft: '15 118 110', strong: '15 118 110', ink: '255 255 255', ac2: '2 132 199' },
    };
    let userAccent = { color: DEFAULT_ACCENT, tint: 'subtle' };
    // While the colour picker is being dragged the accent is only previewed; it is saved when the picker closes.
    let accentPreviewTimer = null;
    let accentPreviewing = false;

    function hexToHsl(hex) {
      const n = parseInt(hex.slice(1), 16);
      const r = (n >> 16 & 255) / 255, g = (n >> 8 & 255) / 255, b = (n & 255) / 255;
      const max = Math.max(r, g, b), min = Math.min(r, g, b);
      const l = (max + min) / 2;
      const d = max - min;
      if (d === 0) return [0, 0, l];
      const sat = d / (1 - Math.abs(2 * l - 1));
      let h;
      if (max === r) h = ((g - b) / d) % 6;
      else if (max === g) h = (b - r) / d + 2;
      else h = (r - g) / d + 4;
      return [(h * 60 + 360) % 360, sat, l];
    }

    // One rule per theme; the light one is more specific, so it wins in light mode.
    function userAccentCss(color, tint) {
      const tintSat = ACCENT_TINTS[tint] ?? ACCENT_TINTS.subtle;
      if (color === DEFAULT_ACCENT && !tintSat) return '';
      const [h, sat, l] = hexToHsl(color);
      const hue = Math.round(h);
      // Progress bars sit on dark poster art in both themes, so they use the bright accent.
      const artAccent = color === DEFAULT_ACCENT
        ? STOCK_ACCENT.dark.ac
        : hslToRgb(h, Math.min(0.9, Math.max(0.4, sat)), Math.min(0.72, Math.max(0.55, l))).join(' ');
      const rule = (mode) => {
        const light = mode === 'light';
        const pal = paletteFromHues([hue, sat, (hue + 40) % 360, hue, tintSat], mode);
        let accent = STOCK_ACCENT[mode];
        if (color !== DEFAULT_ACCENT && light) {
          const [, seedChroma, seedHue] = rgbToOklch(hexToRgb(color));
          accent = lightAccentRoles(seedHue, seedChroma, (seedHue + 40) % 360);
        } else if (color !== DEFAULT_ACCENT) {
          const s = Math.min(0.9, Math.max(0.4, sat));
          const lightness = Math.min(0.72, Math.max(0.55, l));
          const strong = hslToRgb(h, s, lightness - 0.08);
          accent = {
            ac: hslToRgb(h, s, lightness).join(' '),
            soft: hslToRgb(h, s, Math.min(0.88, lightness + 0.13)).join(' '),
            strong: strong.join(' '),
            ink: relativeLuminance(strong) < 0.2 ? '255 255 255' : '8 10 14',
            ac2: pal.ac2,
          };
        }
        const vars = {
          '--ac': accent.ac, '--ac-soft': accent.soft, '--ac-strong': accent.strong, '--ac-ink': accent.ink, '--ac2': accent.ac2, '--ac-art': artAccent,
        };
        if (tintSat) {
          Object.assign(vars, {
            '--bg': pal.bg, '--sunken': pal.sunken, '--chrome': pal.chrome, '--surface': pal.surface, '--field': pal.field,
            '--raised': pal.raised, '--raised2': pal.raised2, '--line': pal.line, '--line-soft': pal.lineSoft,
            '--line-hover': pal.lineHover, '--line-strong': pal.lineStrong,
          });
        }
        return (light ? ':root[data-theme="light"]' : ':root') + '{' + Object.keys(vars).map(k => k + ':' + vars[k]).join(';') + '}';
      };
      return rule('dark') + rule('light');
    }

    function applyUserAccent(color, tint, { persist = true, render = true } = {}) {
      userAccent = { color, tint };
      const css = userAccentCss(color, tint);
      let el = document.getElementById('user-accent');
      if (!el) {
        el = document.createElement('style');
        el.id = 'user-accent';
        document.head.appendChild(el);
      }
      el.textContent = css;
      if (persist) {
        try {
          if (css) localStorage.setItem(ACCENT_CSS_KEY, css);
          else localStorage.removeItem(ACCENT_CSS_KEY);
        } catch (err) {
          // The cache only avoids a flash on load.
        }
      }
      if (render) renderAppearancePicker();
    }

    // The colour swatches and the custom picker; the handlers are the names of the functions to call.
    function accentSwatchesHtml(selected, { pick, preview, commit }) {
      const isPreset = ACCENT_PRESETS.some(p => p[1] === selected);
      const ring = (active) => active ? 'ring-2 ring-offset-2 ring-offset-surface ring-zinc-100' : 'hover:ring-2 hover:ring-zinc-100/30 hover:ring-offset-2 hover:ring-offset-surface';
      return ACCENT_PRESETS.map(([name, hex]) => `
        <button type="button" title="${name}" aria-label="${name}" onclick="${pick}('${hex}')" style="background:${hex}" class="w-6 h-6 rounded-full transition-transform ${ring(hex === selected)}"></button>`).join('') + `
        <label title="Custom colour" class="relative w-6 h-6 rounded-full cursor-pointer overflow-hidden transition-transform ${ring(!isPreset)}" style="background:${isPreset ? 'conic-gradient(#fb7185, #fbbf24, #a3e635, #2dd4bf, #818cf8, #fb7185)' : selected}">
          <input type="color" value="${selected}" oninput="${preview}(this)" onchange="${commit}(this.value)" class="absolute inset-0 w-full h-full opacity-0 cursor-pointer">
        </label>`;
    }

    function renderAppearancePicker() {
      const box = document.getElementById('accent-swatches');
      if (!box) return;
      box.innerHTML = accentSwatchesHtml(userAccent.color, { pick: 'setUserAccent', preview: 'previewUserAccent', commit: 'commitUserAccent' });
      Object.keys(ACCENT_TINTS).forEach(name => {
        const button = document.getElementById(`btn-tint-${name}`);
        if (button) button.className = name === userAccent.tint ? SEG_ACTIVE_CLASS : SEG_INACTIVE_CLASS;
      });
    }

    async function saveAppearance(color, tint) {
      const previous = userAccent;
      applyUserAccent(color, tint);
      try {
        await apiFetch('/api/settings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ accent_color: color, accent_tint: tint })
        });
        if (currentSettings) {
          currentSettings.accent_color = color;
          currentSettings.accent_tint = tint;
        }
      } catch (err) {
        applyUserAccent(previous.color, previous.tint);
        showToast(`Failed to save the colour: ${err.message || err}`, 'error');
      }
    }

    function setUserAccent(color) {
      saveAppearance(color.toLowerCase(), userAccent.tint);
    }

    // Applies the colour once the hex has stayed put for 100 ms. The picker is not re-rendered
    // here: that would replace the input and close the picker mid-drag.
    function previewUserAccent(input) {
      accentPreviewing = true;
      const swatch = input.closest('label');
      if (swatch) swatch.style.background = input.value;
      clearTimeout(accentPreviewTimer);
      accentPreviewTimer = setTimeout(() => {
        applyUserAccent(input.value.toLowerCase(), userAccent.tint, { persist: false, render: false });
      }, 100);
    }

    function commitUserAccent(color) {
      clearTimeout(accentPreviewTimer);
      accentPreviewing = false;
      setUserAccent(color);
    }

    function setAccentTint(tint) {
      saveAppearance(userAccent.color, tint);
    }

    const accentTried = new Set();
    const accentInFlight = new Set();

    async function fetchShowAccent(showId) {
      if (accentInFlight.has(showId)) return;
      accentInFlight.add(showId);
      try {
        const result = await apiFetch(`/api/shows/${showId}/accent`);
        const show = allShows.find(s => s.id === showId);
        if (!show) return;
        show.accent_hues = result.accent_hues;
        show.accent_ready = result.accent_ready;
        if (currentInspectedShowId === showId) renderShowHeader(show);
      } catch (err) {
        // Colours are decoration; the page works without them.
      } finally {
        accentInFlight.delete(showId);
      }
    }

    // Reads the colours of every show in the background, so they are usually ready
    // before a show is opened. Each show is tried once per page load.
    async function warmAccents() {
      for (const show of [...allShows]) {
        if (show.accent_ready || accentTried.has(show.id) || !(show.banner_image || show.cover_image)) continue;
        accentTried.add(show.id);
        await fetchShowAccent(show.id);
      }
    }

    // Runs `swap` inside a view transition when the browser has them, morphing
    // `fromEl` into whatever `toEl()` returns once the swap has happened. `kind` is
    // 'open' or 'close' and picks the direction of the page motion. Without support
    // the swap happens and the incoming section animates with plain CSS.
    //
    // Only one transition runs at a time. A second request (Escape while the page is
    // still opening, a click while it is closing) skips the running one, waits for
    // its clean-up to finish and only then names the poster and starts its own.
    // Starting both at once let the first one's clean-up clear the name the second
    // had just set, so the exit lost its poster morph.
    let renderHold = null;
    let activeTransition = null;

    async function runPageTransition(swap, fromEl, toEl, kind) {
      const NAME = 'show-poster';
      const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      if (activeTransition) {
        const previous = activeTransition;
        previous.transition.skipTransition();
        await previous.done;
      }
      if (reduce) {
        swap();
        return;
      }
      if (!document.startViewTransition) {
        swap();
        const entering = document.getElementById(kind === 'open' ? 'tab-show' : 'main-scroll-container');
        entering.classList.remove('page-enter');
        void entering.offsetWidth;
        entering.classList.add('page-enter');
        setTimeout(() => entering.classList.remove('page-enter'), 400);
        return;
      }
      const root = document.documentElement;
      if (fromEl && fromEl.offsetParent === null) fromEl = null;
      if (fromEl) fromEl.style.viewTransitionName = NAME;
      root.classList.add(`vt-${kind}`);
      let target = null;
      const current = {};
      const transition = document.startViewTransition(() => {
        if (fromEl) fromEl.style.viewTransitionName = '';
        swap();
        target = fromEl && toEl ? toEl() : null;
        if (target && target.offsetParent !== null) target.style.viewTransitionName = NAME;
        else target = null;
      });
      current.transition = transition;
      current.done = transition.finished.catch(() => {}).then(() => {
        root.classList.remove(`vt-${kind}`);
        if (fromEl) fromEl.style.viewTransitionName = '';
        if (target) target.style.viewTransitionName = '';
        if (activeTransition === current) activeTransition = null;
        if (renderHold === current.done) renderHold = null;
      });
      activeTransition = current;
      // loadShows redraws every card; doing that mid-transition would delete the
      // card being morphed into and cancel the animation, so it waits.
      renderHold = current.done;
      return current.done;
    }

    function openShowPage(showId, opts = {}) {
      const container = document.getElementById('main-scroll-container');
      const first = currentInspectedShowId === null;
      if (first) {
        showReturnTab = activeTab === 'calendar' ? 'calendar' : 'shows';
        showReturnScroll = container.scrollTop;
      }
      const card = first && !opts.instant ? document.querySelector(`[data-poster-id="${showId}"]`) : null;

      currentInspectedShowId = showId;
      showContext = null;
      showFeedState = null;
      showInitialState = null;
      showListTab = 'episodes';
      showSideTab = 'feed';
      updateSaveBar();

      if (opts.push !== false) {
        history.pushState({ show: showId }, '', `#show/${showId}`);
        showPagePushed = true;
      } else {
        showPagePushed = opts.fromHistory === true;
      }

      const swap = () => {
        const section = document.getElementById('tab-show');
        container.classList.add('hidden');
        document.body.classList.add('show-open');
        section.classList.remove('hidden');
        section.scrollTop = 0;
        toggleSidebar(false);

        const known = allShows.find(sh => sh.id === showId);
        const titleEl = document.getElementById('page-title');
        if (titleEl) titleEl.textContent = known ? known.display_name : 'Show';
        const block = document.getElementById('show-title-block');
        block.dataset.sig = '';
        renderShowHeader(known);
        loadShowPageBody(showId, true);
      };
      if (opts.instant) swap();
      else runPageTransition(swap, card, () => document.getElementById('show-poster'), 'open');
    }

    async function loadShowPageBody(showId, withSkeleton) {
      const pending = takeShowFetch(showId);
      // Prefetched on hover: draw the body right away, inside the page swap, so it
      // is part of the first frame of the animation.
      if (withSkeleton && pending.ready && allFeeds.length) {
        renderShowBody(showId, pending.data, pending.episodeList);
        return;
      }
      if (withSkeleton) showBodySkeleton();
      try {
        // The rule panel and the episode ledger are independent, so fetch both.
        const [data, episodes] = await Promise.all([pending.rule, pending.episodes]);
        if (!allFeeds.length) {
          try {
            allFeeds = await apiFetch('/api/feeds');
          } catch (err) {
            // The dropdown then only offers Auto-discover.
          }
        }
        if (currentInspectedShowId !== showId) return;
        renderShowBody(showId, data, Array.isArray(episodes) ? episodes : []);
      } catch (err) {
        if (currentInspectedShowId !== showId) return;
        document.getElementById('show-side-form').innerHTML = '';
        setShowListsHtml(`<div class="card p-4 flex-1 flex items-center justify-center text-rose-400 text-sm text-center">Failed loading show details: ${escapeHtml(err.message || err)}</div>`);
      }
    }

    function renderShowBody(showId, data, episodes) {
      const isCompleted = (data.status === 'completed');
      const isPaused = (data.status === 'paused');
      const isRuleActive = data.enabled === true;
      const isDirect = data.download_mode === 'direct';

      let ruleStatusText = '';
      if (isDirect) ruleStatusText = '';
      else if (isCompleted) ruleStatusText = 'Completed';
      else if (isPaused) ruleStatusText = 'Paused';
      else if (isRuleActive) ruleStatusText = 'Rule enabled';
      else ruleStatusText = 'Waiting for air date';

      const isUpcoming = data.is_upcoming === true;
      const isTesting = data.status === 'unconfirmed' && !isUpcoming && !data.feed_locked;

      // Auto-discover only applies while the feed is genuinely undecided: no
      // release seen, and nothing pinned. A feed that has already delivered is
      // decided, and the selector must show it rather than hiding it.
      const isAutoManaged = !isCompleted && !data.feed_locked && !data.has_learned_pattern;
      const selectedFeedId = isAutoManaged ? 0 : (data.current_feed_id || 0);
      const feedLabel = isUpcoming ? 'Feed (not decided yet)' : isTesting ? 'Feed (auto)' : 'Assigned feed';

      const feedOptions = `<option value="0">Auto-discover</option>` +
        allFeeds.map(f => `<option value="${f.id}" ${f.id === selectedFeedId ? 'selected' : ''}>#${f.priority} ${escapeHtml(f.qbit_feed_name)}</option>`).join('');

      // One hint line, the most specific one that applies.
      let feedHint = '';
      if (data.feed_learned) {
        feedHint = `Locked to ${escapeHtml(data.learned_feed_name || 'the delivering feed')} after a download.`;
      } else if (data.feed_pinned) {
        feedHint = 'Pinned by you.';
      } else if (data.candidate_feed_id) {
        feedHint = `May move to ${escapeHtml(data.candidate_feed_name || 'another feed')}, which posted first.`;
      } else if (!isCompleted && data.has_rule && (isUpcoming || isTesting)) {
        feedHint = isUpcoming ? 'Auto: first feed to post wins.'
          : data.waiting_for_release ? 'Auto: waiting for a release.'
          : 'Auto: testing feeds by priority.';
      }
      const feedUrlTitle = (data.feed_url && !isAutoManaged) ? escapeHtml(data.feed_url) : '';

      const noRulePlaceholder = isCompleted
        ? '<p class="field-hint !mt-0">Completed. The rule is disabled.</p>'
        : isDirect
          ? '<p class="field-hint !mt-0">No release found yet. The feed cache is checked now and on every check.</p>'
          : '<p class="field-hint !mt-0">The rule arms when the first episode airs.</p>';

      const aliasesField = `
        <div>
          <label for="show-aliases" class="field-label">Custom aliases</label>
          <input type="text" id="show-aliases" value="${escapeHtml((data.custom_aliases || []).join(', '))}" placeholder="Comma separated" class="field font-mono">
        </div>`;

      // Rules mode: the two patterns are the qBittorrent rule itself. Direct mode
      // matches by the learned name and the aliases, so it only shows what it learned.
      const rulePatternFields = `
        <div>
          <label for="show-must-contain" class="field-label">Must contain (regex)</label>
          <input type="text" id="show-must-contain" value="${escapeHtml(data.must_contain || '')}" placeholder=".*" class="field font-mono">
        </div>
        <div>
          <label for="show-must-not-contain" class="field-label">Must not contain</label>
          <input type="text" id="show-must-not-contain" value="${escapeHtml(data.must_not_contain || '')}" placeholder="(720p|480p|...)" class="field font-mono">
        </div>`;
      const readonlyField = (label, value) => `
        <div>
          <span class="field-label">${label}</span>
          <input type="text" readonly value="${escapeHtml(value)}" onfocus="this.select()" class="field font-mono" title="${escapeHtml(value)}">
        </div>`;
      const matchedAsField = data.matched_as
        ? readonlyField('Matched as', data.matched_as)
        : data.match_pattern
          ? `${readonlyField('Looking for', data.match_pattern)}<p class="field-hint !mt-0">${data.feed_name ? `Nothing in ${escapeHtml(data.feed_name)} matches this show yet. ` : ''}Titles that miss this pattern still match if they are close to an alias.</p>`
          : `<div><span class="field-label">Matched as</span><p class="field-hint !mt-0">${data.feed_name ? `Nothing in ${escapeHtml(data.feed_name)} matches this show yet.` : 'Matched by name and aliases until a release appears.'}</p></div>`;
      const matchingFields = isDirect ? matchedAsField : rulePatternFields;

      // What the feed numbers the season from, when it continues a previous season.
      const shownShow = allShows.find(s => s.id === showId);
      const mapping = isDirect && shownShow ? (shownShow.episode_mappings || []).find(m => m.feed_id === data.current_feed_id) : null;
      const numberingHint = mapping && mapping.offset > 0 ? `This feed numbers the season from episode ${mapping.offset + 1}.` : '';
      const ruleHint = !isDirect && data.has_rule && data.rule_name
        ? `${ruleStatusText}. <span class="font-mono break-words select-all">${escapeHtml(data.rule_name)}</span>`
        : '';

      const linkClass = 'text-sm text-zinc-400 hover:text-zinc-100 underline underline-offset-4 decoration-accent/50 hover:decoration-accent transition-colors';
      document.getElementById('show-side-form').innerHTML = `
        <div class="pane-head">
          <div class="seg">
            <button type="button" id="show-side-tab-feed" onclick="setShowSideTab('feed')" class="${showSideTab === 'feed' ? SEG_ACTIVE_CLASS : SEG_INACTIVE_CLASS}">Feed</button>
            <button type="button" id="show-side-tab-download" onclick="setShowSideTab('download')" class="${showSideTab === 'download' ? SEG_ACTIVE_CLASS : SEG_INACTIVE_CLASS}">Download</button>
          </div>
          <button type="button" id="show-side-reset" onclick="showTriggerRediscover()" class="${linkClass} text-xs ${showSideTab === 'feed' ? '' : 'hidden'}" title="Clear manual rule customizations and let the supervisor auto-match against feeds">Reset to auto-discover</button>
        </div>
        <div id="show-side-feed" class="min-h-0 flex flex-col ${showSideTab === 'feed' ? '' : 'hidden'}">
          <div class="card p-4 min-h-0 lg:overflow-y-auto flex flex-col gap-3">
            <div>
              <label for="show-feed-id" class="field-label">${feedLabel}</label>
              <select id="show-feed-id" class="field" title="${feedUrlTitle}">
                ${feedOptions}
              </select>
              ${feedHint ? `<p class="field-hint">${feedHint}</p>` : ''}
              ${numberingHint ? `<p class="field-hint">${numberingHint}</p>` : ''}
              ${ruleHint ? `<p class="field-hint">${ruleHint}</p>` : ''}
            </div>
            ${data.has_rule ? `${aliasesField}${matchingFields}` : noRulePlaceholder}
          </div>
        </div>
        <div id="show-side-download" class="min-h-0 flex flex-col ${showSideTab === 'download' ? '' : 'hidden'}">
          <div class="card p-4 min-h-0 lg:overflow-y-auto space-y-4">
            <div>
              <label for="show-save-path" class="field-label">Save path</label>
              <input type="text" id="show-save-path" value="${escapeHtml(data.save_path || data.save_folder || '')}" placeholder="~/Anime/${escapeHtml(data.display_name)}" class="field font-mono" title="${escapeHtml(data.save_path || '')}">
            </div>
            <div class="grid grid-cols-2 gap-3">
              <div>
                <label for="show-category" class="field-label">Category</label>
                <input type="text" id="show-category" value="${escapeHtml(data.category || '')}" placeholder="${escapeHtml(currentSettings.default_category || 'anime')}" class="field font-mono">
              </div>
              <div>
                <label for="show-ratio-limit" class="field-label">Seed ratio</label>
                <input type="number" step="0.1" min="0" id="show-ratio-limit" value="${data.ratio_limit !== undefined && data.ratio_limit !== null ? data.ratio_limit : ''}" placeholder="${currentSettings.default_seed_ratio || 1.0}" class="field font-mono">
              </div>
            </div>
          </div>
        </div>`;

      // Whatever the form renders is the unmodified state.
      showInitialState = readShowForm();
      showContext = { showId, data, episodes };
      setShowListsHtml(showListsMarkup(showId, data, episodes, null));
      updateSaveBar();
      loadShowFeedMatches(showId);
      scheduleShowPoll(showId);
    }

    function readShowForm() {
      const val = (id) => {
        const el = document.getElementById(id);
        return el ? el.value : null;
      };
      const feed = val('show-feed-id');
      const ratio = val('show-ratio-limit');
      return {
        current_feed_id: feed !== null ? parseInt(feed) : 0,
        save_folder: (val('show-save-path') || '').trim(),
        category: (val('show-category') || '').trim(),
        ratio_limit: ratio !== null && ratio !== '' ? parseFloat(ratio) : undefined,
        must_contain: (val('show-must-contain') || '').trim(),
        must_not_contain: (val('show-must-not-contain') || '').trim(),
        aliases: (val('show-aliases') || '').trim(),
      };
    }

    function isShowFormDirty() {
      if (!showInitialState) return false;
      const now = readShowForm();
      return Object.keys(showInitialState).some(k => now[k] !== showInitialState[k]);
    }

    function updateSaveBar() {
      const bar = document.getElementById('show-save-bar');
      if (!bar) return;
      const dirty = isShowFormDirty();
      bar.classList.toggle('opacity-0', !dirty);
      bar.classList.toggle('translate-y-3', !dirty);
      bar.classList.toggle('pointer-events-none', !dirty);
      bar.inert = !dirty;
      bar.setAttribute('aria-hidden', String(!dirty));
    }

    function confirmDiscardShowChanges() {
      return !isShowFormDirty() || confirm('Discard unsaved changes?');
    }

    function discardShowChanges() {
      if (!showContext) return;
      renderShowBody(showContext.showId, showContext.data, showContext.episodes);
    }

    // Drops the open page's state and hides it; the caller decides which tab comes next.
    function clearShowPageState() {
      currentInspectedShowId = null;
      showContext = null;
      showFeedState = null;
      showInitialState = null;
      showPagePushed = false;
      showLeaving = false;
      updateSaveBar();
      document.getElementById('tab-show').classList.add('hidden');
      document.body.classList.remove('show-open');
      applyShowPalette(null);
      document.getElementById('main-scroll-container').classList.remove('hidden');
    }

    let showLeaving = false;
    let ignoreNextPop = false;

    // Animated exit. Going to the tab the show was opened from morphs the poster back
    // into its card; any other tab just cross-fades.
    function leaveShowPage(tab = showReturnTab) {
      if (showLeaving) return Promise.resolve();
      showLeaving = true;
      const showId = currentInspectedShowId;
      const morph = tab === showReturnTab;
      const scroll = morph ? showReturnScroll : 0;
      return runPageTransition(() => {
        clearShowPageState();
        switchTab(tab, { animate: false });
        document.getElementById('main-scroll-container').scrollTop = scroll;
      }, morph ? document.getElementById('show-poster') : null,
      () => document.querySelector(`[data-poster-id="${showId}"]`), 'close');
    }

    // The back arrow and Escape. The animated exit starts straight from the click;
    // the history entry we pushed is unwound once it has finished, and that popstate
    // is ignored.
    // A page loaded straight onto #show/<id> has no entry to unwind.
    function closeShowPage() {
      if (currentInspectedShowId === null || showLeaving) return;
      if (!confirmDiscardShowChanges()) return;
      showInitialState = null;
      const pushed = showPagePushed;
      leaveShowPage().then(() => {
        if (pushed) {
          ignoreNextPop = true;
          history.back();
        } else {
          clearShowHash();
        }
      });
    }

    async function saveShowPage() {
      const showId = currentInspectedShowId;
      if (!showId || !isShowFormDirty()) return;

      const now = readShowForm();
      const init = showInitialState;

      // Picking another feed for a show locked to the one that delivered is an
      // explicit override; confirm it, then tell the server to release the lock.
      const feedChanged = now.current_feed_id !== init.current_feed_id;
      const inspectedShow = allShows.find(s => s.id === showId);
      let releaseLearnedFeed = false;
      // Choosing a feed cancels what is still downloading from the other ones (finished
      // downloads stay); the server does it, the confirmation just says so.
      const unfinishedElsewhere = (inspectedShow && feedChanged && now.current_feed_id > 0)
        ? Object.entries(inspectedShow.unfinished_downloads_by_feed || {})
            .filter(([feedId]) => Number(feedId) !== now.current_feed_id)
            .reduce((sum, [, count]) => sum + count, 0)
        : 0;
      const cancelNote = unfinishedElsewhere > 0
        ? `

${unfinishedElsewhere} unfinished download(s) from the old feed will be canceled and fetched again from the new one.`
        : '';
      if (feedChanged && inspectedShow && inspectedShow.feed_learned && now.current_feed_id !== inspectedShow.learned_feed_id) {
        const lockedName = inspectedShow.learned_feed_name || 'the feed that delivered';
        const target = allFeeds.find(f => f.id === now.current_feed_id);
        const targetName = target ? `'${target.qbit_feed_name}'` : 'auto-detect';
        const ok = confirm(
          `'${inspectedShow.display_name}' is locked to '${lockedName}' because a release was recorded from it.

` +
          `Move it to ${targetName} anyway? The next release downloaded will lock the show to that feed.` + cancelNote
        );
        if (!ok) return;
        releaseLearnedFeed = true;
      } else if (cancelNote && !confirm(`Change the feed for '${inspectedShow.display_name}'?` + cancelNote)) {
        return;
      }

      const btn = document.getElementById('btn-save-show');
      btn.disabled = true;
      btn.textContent = 'Saving...';

      const parsedAliases = now.aliases
        ? now.aliases.split(',').map(a => a.trim()).filter(a => a.length > 0)
        : [];

      const payload = {
        // Only sent when the feed was actually changed: an explicit pick pins the
        // feed, while saving other fields must leave auto-detect in charge.
        current_feed_id: feedChanged ? now.current_feed_id : undefined,
        release_learned_feed: releaseLearnedFeed || undefined,
        // The field shows the resolved path, so it is only sent when edited;
        // otherwise every save would pin the show to today's absolute path.
        // An emptied field resets the show to the default folder.
        save_folder: now.save_folder !== init.save_folder ? now.save_folder : undefined,
        category: now.category || undefined,
        ratio_limit: now.ratio_limit,
        must_contain: now.must_contain !== init.must_contain ? now.must_contain : undefined,
        must_not_contain: now.must_not_contain !== init.must_not_contain ? now.must_not_contain : undefined,
        aliases: now.aliases !== init.aliases ? parsedAliases : undefined,
      };

      try {
        const data = await apiFetch(`/api/shows/${showId}/edit`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        showToast(data.message || 'Show updated.', 'success');
        // Redraw from the saved values, which also resets the dirty state.
        await Promise.all([loadShows(), loadShowPageBody(showId, false)]);
      } catch (err) {
        showToast(`Save failed: ${err.message || err}`, 'error');
      } finally {
        btn.disabled = false;
        btn.textContent = 'Save changes';
      }
    }

    async function showTriggerRediscover() {
      const showId = currentInspectedShowId;
      if (!showId) return;
      const show = allShows.find(s => s.id === showId);
      let force = false;
      if (show && show.feed_learned) {
        const feedName = show.learned_feed_name || show.current_feed_name || 'the feed that delivered';
        const ok = confirm(
          `'${show.display_name}' already downloaded a release from '${feedName}'.

` +
          `That feed is locked and is what the app will keep using. Resetting abandons it and ` +
          `re-discovers from scratch, which may move the show to a different feed.

` +
          `Continue anyway?`
        );
        if (!ok) return;
        force = true;
      }
      await rediscoverShow(showId, force);
      if (currentInspectedShowId === showId) loadShowPageBody(showId, false);
    }

    // Moves the show to a feed that carries its releases. The save does the rest:
    // the lock confirmation, the canceled-downloads note and the pin.
    function useFeedFromReleases(feedId) {
      const select = document.getElementById('show-feed-id');
      if (!select) return;
      select.value = String(feedId);
      return saveShowPage();
    }

    function searchPastReleases() {
      const showId = currentInspectedShowId;
      if (!showId) return;
      return once(`search-releases-${showId}`, async () => {
        rerenderShowLists();
        try {
          const data = await apiFetch(`/api/shows/${showId}/search-releases`, { method: 'POST' });
          const episodes = (data.missing || []).join(', ');
          showToast(
            !data.searched ? (data.reason === 'deferred'
                ? 'AniList is rate-limiting right now; try again in a minute.'
                : data.reason === 'no_schedule'
                ? "AniList has no air dates for this show yet; try again later."
                : 'Every aired episode is already in the feeds or downloaded.')
              : data.added && data.paused ? `Found ${data.added} past release(s); resume the show to download them.`
              : data.added && !data.direct ? `Found ${data.added} past release(s); see the Releases tab.`
              : data.added ? `Found ${data.added} past release(s); checking for episodes to download.`
              : `Searched the feeds for episode ${episodes}; nothing found.`,
            data.added ? 'success' : 'info'
          );
          if (data.added && currentInspectedShowId === showId) {
            setShowListTab('feed');
            await refreshShowLists(showId);
          }
        } catch (err) {
          showToast(`Search failed: ${err.message || err}`, 'error');
        } finally {
          actionsInFlight.delete(`search-releases-${showId}`);
          if (currentInspectedShowId === showId) rerenderShowLists();
        }
      });
    }

    async function quickDownloadMatch(showId, btn) {
      const title = btn.dataset.title;
      if (!title) return;
      const key = pendingKey(showId, title);
      if (pendingDownloads.has(key)) return;
      pendingDownloads.add(key);

      btn.disabled = true;
      btn.classList.add('opacity-50', 'pointer-events-none');

      try {
        const data = await apiFetch(`/api/shows/${showId}/quick-download`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title })
        });
        showToast(data.message || 'Download started.', 'success');
        await refreshShowLists(showId);
      } catch (err) {
        showToast(err.message || 'Download failed.', err.status === 409 ? 'info' : 'error');
      } finally {
        pendingDownloads.delete(key);
        btn.disabled = false;
        btn.classList.remove('opacity-50', 'pointer-events-none');
        rerenderShowLists();
      }
    }

    async function restoreEpisode(showId, episodeId, btn) {
      const key = pendingKey(showId, `restore:${episodeId}`);
      if (pendingDownloads.has(key)) return;
      pendingDownloads.add(key);

      btn.disabled = true;
      btn.classList.add('opacity-50', 'pointer-events-none');

      try {
        const data = await apiFetch(`/api/shows/${showId}/episodes/${episodeId}/restore`, { method: 'POST' });
        showToast(data.message || 'Restored.', 'success');
        await refreshShowLists(showId);
      } catch (err) {
        showToast(err.message || 'Restore failed.', err.status === 409 ? 'info' : 'error');
      } finally {
        pendingDownloads.delete(key);
        btn.disabled = false;
        btn.classList.remove('opacity-50', 'pointer-events-none');
        rerenderShowLists();
      }
    }

    function togglePauseShow(id) {
      return once(`pause-${id}`, async () => {
        try {
          const data = await apiFetch(`/api/shows/${id}/pause`, { method: 'POST' });
          showToast(data.message, 'success');
          loadShows();
        } catch (err) {
          showToast(`Action failed: ${err.message || err}`, 'error');
        }
      });
    }

    let allPaused = false;

    function togglePause() {
      const resuming = allPaused;
      return once('pause-all', async () => {
        try {
          const data = await apiFetch(resuming ? '/api/shows/resume-all' : '/api/shows/pause-all', { method: 'POST' });
          showToast(data.message, 'success');
          loadShows();
          updateStatus(true);
        } catch (err) {
          showToast(`Action failed: ${err.message || err}`, 'error');
        }
      });
    }

    function autoDiscoverShows() {
      if (!confirm(`Reset every show to auto-discover?

Hand-picked feeds are cleared. Shows locked to a feed they already downloaded from, and completed shows, are kept.`)) return;
      return once('auto-discover-all', async () => {
        try {
          const data = await apiFetch('/api/shows/auto-discover-all', { method: 'POST' });
          showToast(data.message, data.failed ? 'info' : 'success');
          loadShows();
        } catch (err) {
          showToast(`Action failed: ${err.message || err}`, 'error');
        }
      });
    }

    function rediscoverShow(id, force = false) {
      return once(`rediscover-${id}`, async () => {
        try {
          const data = await apiFetch(`/api/shows/${id}/rediscover${force ? '?force=true' : ''}`, { method: 'POST' });
          showToast(data.message, 'success');
          loadShows();
        } catch (err) {
          showToast(`Action failed: ${err.message || err}`, 'error');
        }
      });
    }

    // The torrents the dialog lists: what the server says deleting removes, or why it could not say.
    function deleteTorrentList(preview) {
      const hint = text => `<p class="field-hint" style="margin-top: 0.75rem">${escapeHtml(text)}</p>`;
      if (!preview || !Array.isArray(preview.torrents)) {
        return hint(preview && preview.error ? preview.error : 'The list of torrents is not available.');
      }
      if (preview.torrents.length === 0) return hint('No torrents in qBittorrent belong to this show.');
      const rows = preview.torrents.map(t => `
        <li class="flex items-baseline gap-2" style="padding: 0.375rem 0.75rem; font-size: 0.75rem">
          <span class="flex-1 min-w-0 truncate text-zinc-400 font-mono" title="${escapeHtml(t.name || t.hash)}">${escapeHtml(t.name || t.hash)}</span>
          ${t.tracked ? '' : '<span class="shrink-0 text-zinc-500">found in its folder</span>'}
        </li>`).join('');
      return `<ul class="rounded-md border border-line divide-y divide-line-soft" style="margin-top: 0.75rem; max-height: 10rem; overflow-y: auto; background: rgb(var(--field))">${rows}</ul>`;
    }

    // Resolves to { deleteFiles } when confirmed, or null when cancelled.
    function confirmDeleteShow(name, preview) {
      return new Promise(resolve => {
        const overlay = document.createElement('div');
        // Spacing is inline: app.css only holds the utility classes the page already uses.
        overlay.className = 'fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4';
        const count = preview && Array.isArray(preview.torrents) ? preview.torrents.length : null;
        const filesLabel = count ? `Also delete the downloaded files of ${count === 1 ? 'this torrent' : `these ${count} torrents`}` : 'Also delete the downloaded files';
        overlay.innerHTML = `
          <div class="card w-full shadow-lg shadow-black/40" style="max-width: 26rem; padding: 1.5rem" role="dialog" aria-modal="true">
            <h3 class="section-title" style="font-size: 0.9375rem; color: rgb(var(--t-title))">Delete '${escapeHtml(name)}'?</h3>
            <p class="field-hint" style="margin-top: 0.5rem">Removes the show, its qBittorrent rule and the torrents listed below, whether they are downloading or seeding.</p>
            ${deleteTorrentList(preview)}
            <label class="flex items-center gap-3 rounded-md border border-line cursor-pointer" style="margin-top: 1.25rem; padding: 0.625rem 0.75rem; font-size: 0.8125rem; color: rgb(var(--t-label)); background: rgb(var(--field))">
              <input type="checkbox" id="delete-files-check" class="rounded bg-canvas border-line text-zinc-300 focus:ring-0">
              ${escapeHtml(filesLabel)}
            </label>
            <div class="flex justify-end gap-3" style="margin-top: 1.5rem">
              <button type="button" data-act="cancel" class="btn">Cancel</button>
              <button type="button" data-act="ok" class="btn btn-danger">Delete</button>
            </div>
          </div>`;
        const finish = result => {
          document.removeEventListener('keydown', onKey);
          overlay.remove();
          resolve(result);
        };
        const onKey = e => { if (e.key === 'Escape') finish(null); };
        overlay.addEventListener('click', e => {
          if (e.target === overlay || e.target.dataset.act === 'cancel') finish(null);
          else if (e.target.dataset.act === 'ok') finish({ deleteFiles: overlay.querySelector('#delete-files-check').checked });
        });
        document.addEventListener('keydown', onKey);
        document.body.appendChild(overlay);
        overlay.querySelector('[data-act="cancel"]').focus();
      });
    }

    async function deleteShow(id) {
      const target = allShows.find(s => s.id === id);
      const name = target ? target.display_name : `#${id}`;
      let preview;
      try {
        preview = await apiFetch(`/api/shows/${id}/delete-preview`);
      } catch (err) {
        preview = { error: `Could not list the torrents: ${err.message || err}` };
      }
      const choice = await confirmDeleteShow(name, preview);
      if (!choice) return;
      await once(`delete-${id}`, async () => {
        try {
          const query = `delete_torrents=true&delete_files=${choice.deleteFiles}`;
          const data = await apiFetch(`/api/shows/${id}?${query}`, { method: 'DELETE' });
          showToast(data.message, 'success');
          loadShows();
        } catch (err) {
          showToast(`Delete failed: ${err.message || err}`, 'error');
        }
      });
    }

    let dragSourceIndex = null;

    function handleDragStart(e, index) {
      dragSourceIndex = index;
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', index);
      e.currentTarget.classList.add('opacity-40', 'bg-raised');
    }

    function handleDragOver(e) {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      e.currentTarget.classList.add('border-t-2', 'border-sky-500', 'bg-surface');
    }

    function handleDragLeave(e) {
      e.currentTarget.classList.remove('border-t-2', 'border-sky-500', 'bg-surface');
    }

    function handleDragEnd(e) {
      e.currentTarget.classList.remove('opacity-40', 'bg-raised');
      document.querySelectorAll('[data-feed-row]').forEach(r => {
        r.classList.remove('border-t-2', 'border-sky-500', 'bg-surface');
      });
    }

    async function handleDrop(e, targetIndex) {
      e.preventDefault();
      e.currentTarget.classList.remove('border-t-2', 'border-sky-500', 'bg-surface');
      if (dragSourceIndex === null || dragSourceIndex === targetIndex) return;
      await moveFeed(dragSourceIndex, targetIndex);
    }

    async function moveFeed(fromIndex, targetIndex) {
      if (targetIndex < 0 || targetIndex >= allFeeds.length || fromIndex === targetIndex) return;

      const movedItem = allFeeds.splice(fromIndex, 1)[0];
      allFeeds.splice(targetIndex, 0, movedItem);

      allFeeds.forEach((f, idx) => { f.priority = idx + 1; });
      renderFeedsTable();
      renderSetupFeeds();

      const reorderPayload = {
        feeds: allFeeds.map((f, idx) => ({ id: f.id, priority: idx + 1 }))
      };

      try {
        await apiFetch('/api/feeds/reorder', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(reorderPayload)
        });
      } catch (err) {
        showToast(`Failed updating priority: ${err}`, 'error');
        if (setupOpen) await setupLoadFeeds(false);
        else loadFeeds();
      } finally {
        dragSourceIndex = null;
      }
    }

    async function loadFeeds() {
      try {
        allFeeds = await apiFetch('/api/feeds');
        renderFeedsTable();
      } catch (err) {
        showToast(`Failed loading feeds: ${err}`, 'error');
      }
    }

    function feedHealthMarkup(f) {
      if (!f.failing_since) return '';
      const since = new Date(f.failing_since);
      const sameDay = since.toDateString() === new Date().toDateString();
      const when = sameDay
        ? since.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
        : since.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
      return `<span class="text-xs text-rose-400" title="qBittorrent has not been able to read this feed since ${escapeHtml(since.toLocaleString())}">Failing since ${escapeHtml(when)}</span>`;
    }

    function renderFeedsTable() {
      const tbody = document.getElementById('feeds-table-body');
      tbody.innerHTML = allFeeds.map((f, idx) => `
        <tr draggable="true" data-feed-row
            ondragstart="handleDragStart(event, ${idx})"
            ondragover="handleDragOver(event)"
            ondragleave="handleDragLeave(event)"
            ondragend="handleDragEnd(event)"
            ondrop="handleDrop(event, ${idx})"
            class="hover:bg-raised/60 cursor-grab active:cursor-grabbing transition-colors group select-none">
          <td class="py-3 px-3 text-center text-zinc-500 group-hover:text-zinc-300">
            <svg class="w-4 h-4 mx-auto opacity-60 group-hover:opacity-100 transition-opacity" fill="currentColor" viewBox="0 0 24 24"><path d="M9 5a2 2 0 11-4 0 2 2 0 014 0zm0 7a2 2 0 11-4 0 2 2 0 014 0zm0 7a2 2 0 11-4 0 2 2 0 014 0zm10-14a2 2 0 11-4 0 2 2 0 014 0zm0 7a2 2 0 11-4 0 2 2 0 014 0zm0 7a2 2 0 11-4 0 2 2 0 014 0z"/></svg>
          </td>
          <td class="py-3 px-2 text-accent text-sm font-medium tabular-nums">
            <div class="flex items-center justify-center gap-1.5">
              <span>#${f.priority}</span>
              <span class="flex flex-col text-zinc-500 opacity-0 group-hover:opacity-100 focus-within:opacity-100 transition-opacity">
                <button onclick="event.stopPropagation(); moveFeed(${idx}, ${idx - 1})" class="px-1 hover:text-zinc-50 disabled:opacity-20" aria-label="Move up" title="Move up" ${idx === 0 ? 'disabled' : ''}><svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M5 15l7-7 7 7"/></svg></button>
                <button onclick="event.stopPropagation(); moveFeed(${idx}, ${idx + 1})" class="px-1 hover:text-zinc-50 disabled:opacity-20" aria-label="Move down" title="Move down" ${idx === allFeeds.length - 1 ? 'disabled' : ''}><svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M19 9l-7 7-7-7"/></svg></button>
              </span>
            </div>
          </td>
          <td class="py-3 px-4" style="width:100%;max-width:0">
            <div class="flex items-baseline gap-3">
              <span class="font-medium text-zinc-100 text-sm">${escapeHtml(f.qbit_feed_name)}</span>
              ${feedHealthMarkup(f)}
            </div>
            <div class="text-xs text-zinc-500 font-mono truncate mt-0.5" title="${escapeHtml(f.qbit_feed_url)}">${escapeHtml(f.qbit_feed_url)}</div>
          </td>
          <td class="py-3 px-3 text-right text-xs text-zinc-500 tabular-nums whitespace-nowrap">
            ${f.shows > 0 ? `<div>${f.shows} show${f.shows === 1 ? '' : 's'}</div>${f.locked > 0 ? `<div class="mt-0.5">${f.locked} locked</div>` : ''}` : ''}
          </td>
          <td class="py-3 px-3">
            <div class="flex items-center justify-end gap-1 opacity-0 group-hover:opacity-100 focus-within:opacity-100 transition-opacity">
              <button draggable="false" onclick="event.stopPropagation(); openFeedDialog(${f.id})" class="w-7 h-7 rounded-md flex items-center justify-center text-zinc-500 hover:text-zinc-50 hover:bg-raised transition-colors active:scale-90" title="Edit feed" aria-label="Edit feed">
                <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2.2"><path stroke-linecap="round" stroke-linejoin="round" d="M16.862 4.487l1.687-1.688a1.875 1.875 0 112.652 2.652L10.582 16.07a4.5 4.5 0 01-1.897 1.13L6 18l.8-2.685a4.5 4.5 0 011.13-1.897l8.932-8.931zm0 0L19.5 7.125M18 14v4.75A2.25 2.25 0 0115.75 21H5.25A2.25 2.25 0 013 18.75V8.25A2.25 2.25 0 015.25 6H10"/></svg>
              </button>
              <button draggable="false" onclick="event.stopPropagation(); deleteFeed(${f.id})" class="w-7 h-7 rounded-md flex items-center justify-center text-zinc-500 hover:text-rose-400 hover:bg-raised transition-colors active:scale-90" title="Delete feed" aria-label="Delete feed">
                <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2.2"><path stroke-linecap="round" stroke-linejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/></svg>
              </button>
            </div>
          </td>
        </tr>
      `).join('') || '<tr><td colspan="5" class="text-center py-10 text-zinc-500 text-sm">No feeds registered.</td></tr>';
    }

    // Add (no argument) or edit (feed id) a feed. qBittorrent is changed first; the
    // server keeps the feed's row, so its priority and locked shows survive an edit.
    function openFeedDialog(feedId = null) {
      const feed = feedId === null ? null : allFeeds.find(f => f.id === feedId);
      if (feedId !== null && !feed) return;
      const overlay = document.createElement('div');
      overlay.className = 'fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4';
      overlay.innerHTML = `
        <form class="card w-full shadow-lg shadow-black/40" style="max-width: 28rem; padding: 1.5rem" role="dialog" aria-modal="true">
          <h3 class="section-title" style="font-size: 0.9375rem; color: rgb(var(--t-title))">${feed ? `Edit '${escapeHtml(feed.qbit_feed_name)}'` : 'Add RSS feed'}</h3>
          <div style="margin-top: 1.25rem">
            <label class="field-label" for="feed-dlg-name">Name</label>
            <input id="feed-dlg-name" type="text" required autocomplete="off" class="field">
          </div>
          <div style="margin-top: 1rem">
            <label class="field-label" for="feed-dlg-url">RSS URL</label>
            <input id="feed-dlg-url" type="url" required autocomplete="off" placeholder="https://…" class="field font-mono">
            <p class="field-hint">${feed ? "Changing the URL keeps this feed's priority, locked shows and cached releases." : 'Added to qBittorrent at the bottom of the list.'}</p>
          </div>
          <p id="feed-dlg-error" class="field-hint hidden" style="color: rgb(var(--danger-fg))" role="alert"></p>
          <div class="flex justify-end gap-3" style="margin-top: 1.5rem">
            <button type="button" data-act="cancel" class="btn">Cancel</button>
            <button type="submit" data-act="save" class="btn btn-primary">${feed ? 'Save' : 'Add'}</button>
          </div>
        </form>`;
      const nameInput = overlay.querySelector('#feed-dlg-name');
      const urlInput = overlay.querySelector('#feed-dlg-url');
      const errorLine = overlay.querySelector('#feed-dlg-error');
      const saveBtn = overlay.querySelector('[data-act="save"]');
      nameInput.value = feed ? feed.qbit_feed_name : '';
      urlInput.value = feed ? feed.qbit_feed_url : '';
      const close = () => {
        document.removeEventListener('keydown', onKey);
        overlay.remove();
      };
      const onKey = e => { if (e.key === 'Escape') close(); };
      overlay.addEventListener('click', e => {
        if (e.target === overlay || e.target.dataset.act === 'cancel') close();
      });
      overlay.querySelector('form').addEventListener('submit', async e => {
        e.preventDefault();
        const name = nameInput.value.trim();
        const url = urlInput.value.trim();
        let request;
        if (feed) {
          const body = {};
          if (name !== feed.qbit_feed_name) body.name = name;
          if (url !== feed.qbit_feed_url) body.url = url;
          if (!Object.keys(body).length) { close(); return; }
          request = { url: `/api/feeds/${feed.id}`, options: { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } };
        } else {
          request = { url: '/api/feeds', options: { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name, url }) } };
        }
        await once('feed-save', async () => {
          saveBtn.disabled = true;
          errorLine.classList.add('hidden');
          try {
            const data = await apiFetch(request.url, request.options);
            close();
            showToast(data.message, 'success');
            loadFeeds();
            loadShows();
          } catch (err) {
            errorLine.textContent = err.message || String(err);
            errorLine.classList.remove('hidden');
            saveBtn.disabled = false;
          }
        });
      });
      document.addEventListener('keydown', onKey);
      document.body.appendChild(overlay);
      nameInput.focus();
    }

    // Resolves to true when confirmed.
    function confirmDeleteFeed(feed) {
      return new Promise(resolve => {
        const overlay = document.createElement('div');
        overlay.className = 'fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4';
        const usage = feed.shows > 0
          ? ` ${feed.shows} show${feed.shows === 1 ? ' uses' : 's use'} it and will go back to Auto-Discover.`
          : '';
        const lockWarning = feed.locked > 0
          ? `<p class="field-hint" style="color: rgb(var(--danger-fg))">${feed.locked} show${feed.locked === 1 ? ' is' : 's are'} locked to this feed; ${feed.locked === 1 ? 'its' : 'their'} lock is released.</p>`
          : '';
        overlay.innerHTML = `
          <div class="card w-full shadow-lg shadow-black/40" style="max-width: 26rem; padding: 1.5rem" role="dialog" aria-modal="true">
            <h3 class="section-title" style="font-size: 0.9375rem; color: rgb(var(--t-title))">Delete '${escapeHtml(feed.qbit_feed_name)}'?</h3>
            <p class="field-hint" style="margin-top: 0.5rem">Removes the feed from qBittorrent and Kisetsu.${usage}</p>
            ${lockWarning}
            <div class="flex justify-end gap-3" style="margin-top: 1.5rem">
              <button type="button" data-act="cancel" class="btn">Cancel</button>
              <button type="button" data-act="ok" class="btn btn-danger">Delete</button>
            </div>
          </div>`;
        const finish = result => {
          document.removeEventListener('keydown', onKey);
          overlay.remove();
          resolve(result);
        };
        const onKey = e => { if (e.key === 'Escape') finish(false); };
        overlay.addEventListener('click', e => {
          if (e.target === overlay || e.target.dataset.act === 'cancel') finish(false);
          else if (e.target.dataset.act === 'ok') finish(true);
        });
        document.addEventListener('keydown', onKey);
        document.body.appendChild(overlay);
        overlay.querySelector('[data-act="cancel"]').focus();
      });
    }

    async function deleteFeed(id) {
      const feed = allFeeds.find(f => f.id === id);
      if (!feed || !(await confirmDeleteFeed(feed))) return;
      await once(`delete-feed-${id}`, async () => {
        try {
          const data = await apiFetch(`/api/feeds/${id}`, { method: 'DELETE' });
          showToast(data.message, 'success');
          loadFeeds();
          loadShows();
        } catch (err) {
          showToast(`Delete failed: ${err.message || err}`, 'error');
        }
      });
    }

    async function syncFeeds() {
      try {
        const data = await apiFetch('/api/feeds/sync', { method: 'POST' });
        showToast(data.message || 'Feeds synced.', 'success');
        loadFeeds();
      } catch (err) {
        showToast(`Sync failed: ${err}`, 'error');
      }
    }

    function updateTitleLanguageUi(lang) {
      const hiddenEl = document.getElementById('set-title-language');
      if (hiddenEl) hiddenEl.value = lang;

      const btnJa = document.getElementById('btn-lang-ja');
      const btnEn = document.getElementById('btn-lang-en');

      if (btnJa && btnEn) {
        if (lang === 'english') {
          btnEn.className = SEG_ACTIVE_CLASS;
          btnJa.className = SEG_INACTIVE_CLASS;
        } else {
          btnJa.className = SEG_ACTIVE_CLASS;
          btnEn.className = SEG_INACTIVE_CLASS;
        }
      }
    }

    function updateDownloadModeUi(mode) {
      const selected = mode || 'rules';
      const hidden = document.getElementById('set-download-mode');
      if (hidden) hidden.value = selected;
      // Only the direct engine refreshes feeds itself while hunting.
      const huntRow = document.getElementById('set-hunt-interval-row');
      if (huntRow) huntRow.classList.toggle('hidden', selected !== 'direct');

      ['rules', 'direct'].forEach(name => {
        const button = document.getElementById(`btn-mode-${name}`);
        if (!button) return;
        const active = name === selected;
        button.className = active ? SEG_ACTIVE_CLASS : SEG_INACTIVE_CLASS;
      });
    }

    async function setDownloadMode(mode) {
      if (!['rules', 'direct'].includes(mode)) return;
      try {
        const data = await apiFetch('/api/settings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ download_mode: mode })
        });
        showToast(data.message || `Download engine switched to ${mode}.`, 'success');
        await loadSettings();
        await loadShows();
        updateStatus(true);
      } catch (err) {
        updateDownloadModeUi((currentSettings && currentSettings.download_mode) || 'rules');
        showToast(`Mode switch failed: ${err.message || err}`, 'error');
      }
    }

    function updateNotificationsUi(on) {
      const btnOn = document.getElementById('btn-notify-on');
      const btnOff = document.getElementById('btn-notify-off');
      if (btnOn) btnOn.className = on ? SEG_ACTIVE_CLASS : SEG_INACTIVE_CLASS;
      if (btnOff) btnOff.className = on ? SEG_INACTIVE_CLASS : SEG_ACTIVE_CLASS;
    }

    async function setDesktopNotifications(on) {
      const previous = !!(currentSettings && currentSettings.desktop_notifications);
      updateNotificationsUi(on);
      try {
        await apiFetch('/api/settings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ desktop_notifications: on })
        });
        if (currentSettings) currentSettings.desktop_notifications = on;
      } catch (err) {
        updateNotificationsUi(previous);
        showToast(`Failed to update notifications: ${err.message || err}`, 'error');
      }
    }

    async function setTitleLanguage(lang) {
      const previous = currentSettings && currentSettings.title_language;
      if (previous === lang) return;
      try {
        const plan = await apiFetch(`/api/settings/title-language-preview?lang=${encodeURIComponent(lang)}`);
        let moveExisting = false;
        if (plan.torrents > 0) {
          const folders = plan.shows.map(s => `${s.old}  ->  ${s.new}`).join('\n');
          const label = lang === 'romaji' ? 'Romaji' : 'English';
          if (!confirm(
            `Switching to ${label} renames the download folders of ${plan.shows.length} shows. ` +
            `Their ${plan.torrents} downloads will move to the new folders.\n\n${folders}\n\nSwitch and move them?`
          )) {
            return;
          }
          moveExisting = true;
        }
        updateTitleLanguageUi(lang);
        await apiFetch('/api/settings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title_language: lang, move_existing: moveExisting })
        });
        if (currentSettings) currentSettings.title_language = lang;
        await loadShows();
      } catch (err) {
        if (previous) updateTitleLanguageUi(previous);
        showToast(`Failed to update title language: ${err.message || err}`, 'error');
      }
    }

    // Only the fields that need the Save bar; the engine, names, theme and accent apply when clicked.
    let settingsInitialState = null;

    function readSettingsForm() {
      const val = (id) => document.getElementById(id).value;
      return {
        qbit_host: val('set-qbit-host'),
        qbit_username: val('set-qbit-user'),
        qbit_password: val('set-qbit-pass'),
        base_dir: val('set-base-dir'),
        default_category: val('set-category'),
        default_seed_ratio: val('set-ratio'),
        stall_wait_hours: val('set-stall-window'),
        anilist_username: val('set-anilist-user'),
        refresh_interval_minutes: val('set-interval'),
        hunt_interval_minutes: val('set-hunt-interval'),
        early_air_tolerance_hours: val('set-early-air-tolerance'),
      };
    }

    function fillSettingsForm() {
      const s = currentSettings || {};
      document.getElementById('set-qbit-host').value = s.qbit_host || '';
      document.getElementById('set-qbit-user').value = s.qbit_username || '';
      document.getElementById('set-qbit-pass').value = '';
      document.getElementById('set-base-dir').value = s.base_dir || '';
      document.getElementById('set-category').value = s.default_category || '';
      document.getElementById('set-ratio').value = s.default_seed_ratio ?? 1.0;
      document.getElementById('set-stall-window').value = s.stall_wait_hours ?? 24;
      document.getElementById('set-anilist-user').value = s.anilist_username || '';
      document.getElementById('set-interval').value = s.refresh_interval_minutes ?? 360;
      document.getElementById('set-hunt-interval').value = s.hunt_interval_minutes ?? 5;
      document.getElementById('set-early-air-tolerance').value = s.early_air_tolerance_hours ?? 6;
      settingsInitialState = readSettingsForm();
      updateSettingsSaveBar();
    }

    function isSettingsDirty() {
      if (!settingsInitialState) return false;
      const now = readSettingsForm();
      return Object.keys(settingsInitialState).some(k => now[k] !== settingsInitialState[k]);
    }

    function updateSettingsSaveBar() {
      const bar = document.getElementById('settings-save-bar');
      if (!bar) return;
      const dirty = isSettingsDirty();
      bar.classList.toggle('opacity-0', !dirty);
      bar.classList.toggle('translate-y-3', !dirty);
      bar.classList.toggle('pointer-events-none', !dirty);
      bar.inert = !dirty;
      bar.setAttribute('aria-hidden', String(!dirty));
    }

    function confirmDiscardSettingsChanges() {
      return !isSettingsDirty() || confirm('Discard unsaved changes?');
    }

    function discardSettingsChanges() {
      fillSettingsForm();
    }

    async function loadSettings() {
      try {
        const s = await apiFetch('/api/settings');
        currentSettings = s;
        // The engine and name buttons reload settings too; edits in the text fields survive that.
        if (!isSettingsDirty()) fillSettingsForm();
        updateTitleLanguageUi(s.title_language || 'english');
        updateDownloadModeUi(s.download_mode || 'rules');
        updateNotificationsUi(!!s.desktop_notifications);
        applyUserAccent(s.accent_color || DEFAULT_ACCENT, s.accent_tint || 'subtle');
      } catch (err) {
        showToast(`Failed loading settings: ${err.message || err}`, 'error');
      }
    }

    async function saveSettings(e) {
      e.preventDefault();
      const payload = {
        qbit_host: document.getElementById('set-qbit-host').value,
        qbit_username: document.getElementById('set-qbit-user').value,
        base_dir: document.getElementById('set-base-dir').value,
        default_category: document.getElementById('set-category').value,
        default_seed_ratio: parseFloat(document.getElementById('set-ratio').value),
        stall_wait_hours: parseInt(document.getElementById('set-stall-window').value),
        anilist_username: document.getElementById('set-anilist-user').value,
        refresh_interval_minutes: parseInt(document.getElementById('set-interval').value),
        hunt_interval_minutes: parseInt(document.getElementById('set-hunt-interval').value),
        early_air_tolerance_hours: parseInt(document.getElementById('set-early-air-tolerance').value),
      };
      const pwd = document.getElementById('set-qbit-pass').value;
      if (pwd) payload.qbit_password = pwd;

      await once('save-settings', async () => {
        try {
          const data = await apiFetch('/api/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
          });
          showToast(data.message || 'Settings saved.', 'success');
          settingsInitialState = null;
          await loadSettings();
          await loadShows();
        } catch (err) {
          showToast(`Failed saving settings: ${err.message || err}`, 'error');
        }
      });
    }

    async function testQbitConnection() {
      const statusEl = document.getElementById('test-qbit-status');
      statusEl.textContent = 'Testing...';
      statusEl.className = 'text-xs font-mono text-zinc-400';
      const payload = {
        qbit_host: document.getElementById('set-qbit-host').value,
        qbit_username: document.getElementById('set-qbit-user').value,
      };
      const pwd = document.getElementById('set-qbit-pass').value;
      if (pwd) payload.qbit_password = pwd;
      try {
        const data = await apiFetch('/api/settings/test-qbit', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        statusEl.textContent = `Connected (qBit ${data.app_version})`;
        statusEl.className = 'text-xs font-mono text-emerald-400 font-semibold';
      } catch (err) {
        statusEl.textContent = err.status ? `${err.message}` : `Failed: ${err.message || err}`;
        statusEl.className = 'text-xs font-mono text-rose-400';
      }
    }

    async function clearAllShows() {
      if (!confirm('Delete ALL monitored shows and remove all qBittorrent RSS rules?')) return;
      await once('clear-all', async () => {
        try {
          const data = await apiFetch('/api/settings/clear-all', { method: 'POST' });
          showToast(data.message || 'All shows cleared.', 'success');
          loadShows();
        } catch (err) {
          showToast(`Clear failed: ${err.message || err}`, 'error');
        }
      });
    }

    async function runCycleNow() {
      const btn = document.getElementById('btn-run-cycle');
      const spinner = document.getElementById('spinner-run-cycle');
      const text = document.getElementById('text-run-cycle');

      btn.disabled = true;
      spinner.classList.remove('hidden');
      text.textContent = 'Syncing...';
      let followUp = false;

      try {
        const data = await apiFetch('/api/cycle/run', { method: 'POST', notice: false });
        showToast(data.message || 'Sync completed.', 'success');
        loadShows();
        if (data.feeds_refreshing) {
          followUp = true;
          watchFeedRefresh();
        }
        updateStatus(true);
      } catch (err) {
        // A busy cycle is a conflict, not a failure: something is already
        // running, so report it as information.
        if (err.status === 409) {
          showToast(err.message || 'A supervision cycle is already in progress.', 'info');
        } else {
          showToast(`Sync error: ${err.message || err}`, 'error');
        }
      } finally {
        btn.disabled = false;
        if (!followUp) {
          spinner.classList.add('hidden');
          text.textContent = 'Sync Now';
        }
      }
    }

    // After a manual sync the feeds are refreshed by the background loop; keep the
    // button showing that until it reports done, or give up after a while.
    let feedWatchTimer = null;
    function watchFeedRefresh() {
      clearTimeout(feedWatchTimer);
      const spinner = document.getElementById('spinner-run-cycle');
      const text = document.getElementById('text-run-cycle');
      spinner.classList.remove('hidden');
      text.textContent = 'Checking feeds...';
      const startedAt = Date.now();
      const finish = () => {
        spinner.classList.add('hidden');
        text.textContent = 'Sync Now';
      };
      const poll = async () => {
        try {
          const st = await apiFetch('/api/status', { notice: false });
          if (!st.feeds_refreshing && !st.is_running_cycle) {
            finish();
            loadShows();
            updateStatus(true);
            const added = st.last_feed_refresh_added;
            showToast(added ? `Feeds checked: ${added} new release(s).` : 'Feeds checked: no new releases.', 'success');
            return;
          }
        } catch {
          // A failed poll is retried until the deadline.
        }
        if (Date.now() - startedAt > 90000) {
          finish();
          return;
        }
        feedWatchTimer = setTimeout(poll, 2000);
      };
      feedWatchTimer = setTimeout(poll, 1500);
    }

    let cachedHistory = [];
    let historyLoadInFlight = false;
    let historyLoadQueued = false;
    let historyManualRefreshQueued = false;


    function formatRelativeTime(isoStr) {
      if (!isoStr) return '';
      const now = Date.now();
      const t = new Date(isoStr).getTime();
      const diffSec = Math.max(0, Math.floor((now - t) / 1000));

      if (diffSec < 60) return 'just now';
      const diffMin = Math.floor(diffSec / 60);
      if (diffMin < 60) return `${diffMin}m ago`;
      const diffHours = Math.floor(diffMin / 60);
      if (diffHours < 24) return `${diffHours}h ago`;
      const diffDays = Math.floor(diffHours / 24);
      return `${diffDays}d ago`;
    }

    async function loadHistory(manual = false) {
      if (historyLoadInFlight) {
        historyLoadQueued = true;
        if (manual) historyManualRefreshQueued = true;
        return;
      }
      historyLoadInFlight = true;
      try {
        cachedHistory = await apiFetch('/api/history?limit=100');
        renderHistory();
      } catch (err) {
        if (manual) showToast(`Failed to load history: ${err.message || err}`, 'error');
      } finally {
        historyLoadInFlight = false;
        if (historyLoadQueued) {
          const queuedManual = manual || historyManualRefreshQueued;
          historyLoadQueued = false;
          historyManualRefreshQueued = false;
          loadHistory(queuedManual);
        }
      }
    }

    function historyDayLabel(isoStr) {
      if (!isoStr) return 'Earlier';
      const d = new Date(isoStr);
      const startOf = x => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
      const days = Math.round((startOf(new Date()) - startOf(d)) / 86400000);
      if (days <= 0) return 'Today';
      if (days === 1) return 'Yesterday';
      return d.toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long' });
    }

    function renderHistory() {
      const container = document.getElementById('history-container');
      const badge = document.getElementById('badge-total-history');
      if (!container) return;

      if (badge) badge.textContent = `${cachedHistory.length}`;

      if (!cachedHistory || cachedHistory.length === 0) {
        container.innerHTML = `
          <div class="card p-10 text-center space-y-1.5">
            <div class="text-zinc-300 text-sm">Nothing here yet</div>
            <p class="text-xs text-zinc-500 max-w-sm mx-auto">Releases show up here once they are added or matched for one of your shows.</p>
          </div>
        `;
        return;
      }

      const groups = [];
      cachedHistory.forEach(h => {
        const label = historyDayLabel(h.created_at);
        const last = groups[groups.length - 1];
        if (last && last.label === label) last.items.push(h);
        else groups.push({ label, items: [h] });
      });

      container.innerHTML = groups.map(g => `
        <div>
          <div class="text-xs font-medium text-zinc-400 mb-1.5 px-1">${escapeHtml(g.label)}</div>
          <div class="card divide-y divide-line-soft">
            ${g.items.map(renderHistoryRow).join('')}
          </div>
        </div>
      `).join('');
    }

    function renderHistoryRow(h) {
      const added = (h.rule_name || '').startsWith('Direct:');
      const verb = added ? 'Added' : 'Matched';
      const fullTime = h.created_at ? new Date(h.created_at).toLocaleString() : '';
      const showName = h.show_name || (h.rule_name || '').replace(/^Direct:\s*/, '');
      // For rules rows the rule and its pattern are the only extra facts; keep
      // them out of the way instead of printing them on every line.
      const ruleTip = (!added && (h.rule_name || h.matched_regex))
        ? `${h.rule_name || ''}${h.matched_regex ? `
${h.matched_regex}` : ''}`
        : '';
      return `
        <div class="px-4 py-3 flex items-center gap-4" ${ruleTip ? `title="${escapeHtml(ruleTip)}"` : ''}>
          <div class="min-w-0 flex-1">
            <div class="flex items-baseline gap-2 min-w-0">
              <span class="text-sm font-medium text-zinc-100 truncate">${escapeHtml(showName)}</span>
              ${h.episode ? `<span class="text-xs text-accent tabular-nums flex-shrink-0">Ep ${escapeHtml(h.episode)}</span>` : ''}
            </div>
            <div class="text-xs text-zinc-500 font-mono truncate mt-0.5 select-all" title="${escapeHtml(h.release_title || '')}">${escapeHtml(h.release_title || '')}</div>
          </div>
          <div class="text-right flex-shrink-0">
            <div class="text-xs text-zinc-400" title="${escapeHtml(fullTime)}">${formatRelativeTime(h.created_at)}</div>
            <div class="text-xs text-zinc-500 mt-0.5 truncate max-w-[12rem]"><span class="${added ? 'text-accent' : 'text-sky-400'}">${verb}</span>${h.feed_name ? ` ${added ? 'from' : 'in'} ${escapeHtml(h.feed_name)}` : ''}</div>
          </div>
        </div>
      `;
    }

    async function clearHistory() {
      if (!confirm('Clear all match history?')) return;
      await once('clear-history', async () => {
        try {
          await apiFetch('/api/history', { method: 'DELETE' });
          showToast('Match history cleared.', 'success');
          loadHistory();
        } catch (err) {
          showToast(`Failed to clear history: ${err.message || err}`, 'error');
        }
      });
    }

    let logsAutoRefreshInterval = null;
    let cachedLogs = [];


    async function loadLogs(manual = false) {
      try {
        cachedLogs = await apiFetch('/api/logs?limit=250');
        renderLogs();
      } catch (err) {
        if (manual) showToast(`Failed to load logs: ${err.message || err}`, 'error');
      }
    }

    function renderLogs() {
      const container = document.getElementById('logs-container');
      const badge = document.getElementById('logs-count-badge');
      if (!container) return;

      badge.textContent = `${cachedLogs.length} entries`;

      if (!cachedLogs || cachedLogs.length === 0) {
        container.innerHTML = '<div class="text-zinc-600 text-center py-12">No activity logged yet.</div>';
        return;
      }

      const rows = cachedLogs.map(l => {
        const level = (l.level || 'INFO').toUpperCase();
        const marks = { ERROR: ['ERROR', 'text-rose-400'], WARNING: ['WARN', 'text-amber-400'], WARN: ['WARN', 'text-amber-400'], DEBUG: ['DEBUG', 'text-sky-400'] };
        const mark = marks[level] ? `<span class="font-semibold ${marks[level][1]} mr-2">${marks[level][0]}</span>` : '';

        const timeStr = localTime(l.timestamp) || l.time_str || '';

        return `<div class="flex items-start gap-3 py-0.5 hover:bg-chrome px-1.5 rounded transition-colors leading-relaxed">
          <span class="text-zinc-500 select-none text-xs font-mono shrink-0">${timeStr}</span>
          <span class="text-zinc-200 break-all select-text font-mono text-xs flex-1">${mark}${escapeHtml(l.message || '')}</span>
        </div>`;
      });

      container.innerHTML = rows.join('');
      container.scrollTop = container.scrollHeight;
    }

    function toggleLogsAutoRefresh(enabled) {
      if (logsAutoRefreshInterval) clearInterval(logsAutoRefreshInterval);
      if (enabled) {
        logsAutoRefreshInterval = setInterval(() => {
          if (activeTab === 'logs') loadLogs();
        }, 4000);
      }
    }

    function copyLogs() {
      if (!cachedLogs || cachedLogs.length === 0) {
        showToast('No logs to copy.', 'info');
        return;
      }
      const text = cachedLogs.map(l => `[${localTime(l.timestamp) || l.time_str || l.timestamp}] [${l.level || 'INFO'}] ${l.message}`).join('\n');
      navigator.clipboard.writeText(text).then(() => {
        showToast('Copied.', 'success');
      }).catch(() => {
        showToast('Failed to copy logs.', 'error');
      });
    }

    let targetNextCheckTime = null;
    let nextCheckReason = '';
    let statusUpdateInFlight = false;
    let statusUpdateQueued = false;

    function formatNextCheckText(seconds) {
      if (seconds === undefined || seconds === null) return 'Calculating...';
      const totalSec = Math.max(0, Math.floor(seconds));
      if (totalSec <= 0) return 'Checking now...';
      if (totalSec < 60) return `in ${totalSec}s`;
      const totalMin = Math.floor(totalSec / 60);
      if (totalMin < 60) return `in ${totalMin}m ${totalSec % 60}s`;
      const h = Math.floor(totalMin / 60);
      const m = totalMin % 60;
      return m > 0 ? `in ${h}h ${m}m` : `in ${h}h`;
    }

    // Set from /api/status while a check holds the cycle slot.
    let runningCheck = null;
    let statusRepoll = null;
    // What /api/status said about the ledger last time; a change means the lists on screen are out of date.
    let ledgerFingerprint = null;
    let anilistBlockedUntil = null;
    let serverReachable = true;

    function renderServerPills() {
      const offline = document.getElementById('stat-offline');
      if (offline) offline.classList.toggle('hidden', serverReachable);
      const anilist = document.getElementById('stat-anilist');
      if (anilist) {
        const paused = serverReachable && anilistBlockedUntil !== null && anilistBlockedUntil > Date.now();
        anilist.classList.toggle('hidden', !paused);
        if (paused) anilist.title = `AniList is limiting requests or not answering. Kisetsu asks again at ${new Date(anilistBlockedUntil).toLocaleTimeString()}; downloads carry on meanwhile.`;
      }
    }

    function tickNextCheck() {
      const nextEl = document.getElementById('sidebar-next-check');
      if (nextEl && runningCheck) {
        const seconds = runningCheck.seconds + Math.round((Date.now() - runningCheck.seenAt) / 1000);
        nextEl.textContent = `Checking… ${seconds}s`;
        const runningTip = `A ${runningCheck.label} is running.`;
        if (nextEl.title !== runningTip) nextEl.title = runningTip;
      } else if (nextEl) {
        if (!targetNextCheckTime) {
          nextEl.textContent = 'Routine check';
        } else {
          const now = Date.now();
          const remSec = Math.max(0, Math.floor((targetNextCheckTime - now) / 1000));
          nextEl.textContent = formatNextCheckText(remSec);

          if (nextCheckReason) {
            const targetDate = new Date(targetNextCheckTime);
            const tip = `${nextCheckReason}
Target Time: ${targetDate.toLocaleTimeString()}`;
            if (nextEl.title !== tip) nextEl.title = tip;
          }
        }
      }
    }

    function tickCountdown() {
      renderServerPills();
      tickNextCheck();

      document.querySelectorAll('.show-countdown[data-air-at]').forEach(el => {
        const airAt = el.getAttribute('data-air-at');
        const dateStr = el.getAttribute('data-date-str');
        if (airAt) {
          el.textContent = formatCardCountdown(formatEpisodeCountdown(airAt), dateStr);
        }
      });
    }

    // The failing feeds the last status answer listed; a change reloads the feed list so its health stays current.
    let lastFailingKey = null;

    async function updateStatus(force = false) {
      if (statusUpdateInFlight) {
        if (force) statusUpdateQueued = true;
        return;
      }
      if (!force && document.hidden) return;
      statusUpdateInFlight = true;
      let answered = false;
      try {
        const st = await apiFetch('/api/status');
        answered = true;
        serverReachable = true;
        const failingKey = (st.failing_feeds || []).join('|');
        if (lastFailingKey !== null && failingKey !== lastFailingKey) loadShows({ quiet: true });
        lastFailingKey = failingKey;
        anilistBlockedUntil = st.anilist_blocked_until ? new Date(st.anilist_blocked_until).getTime() : null;
        nextCheckReason = st.next_check_reason || '';
        if (st.target_next_check_time) {
          targetNextCheckTime = new Date(st.target_next_check_time).getTime();
        } else if (st.next_check_seconds !== undefined && st.next_check_seconds !== null) {
          targetNextCheckTime = Date.now() + (st.next_check_seconds * 1000);
        }

        runningCheck = st.is_running_cycle
          ? { label: st.cycle_label || 'background check', seconds: st.cycle_seconds || 0, seenAt: Date.now() }
          : null;
        clearTimeout(statusRepoll);
        statusRepoll = runningCheck ? setTimeout(() => updateStatus(true), 2000) : null;

        tickCountdown();

        allPaused = !!st.all_paused;
        document.getElementById('stat-all-paused').classList.toggle('hidden', !allPaused);
        const pauseBtn = document.getElementById('btn-pause');
        const pauseLabel = allPaused
          ? 'Resume downloading (shows you paused yourself stay paused)'
          : 'Pause downloading';
        pauseBtn.title = pauseLabel;
        pauseBtn.setAttribute('aria-label', pauseLabel);
        document.getElementById('icon-pause').classList.toggle('hidden', allPaused);
        document.getElementById('icon-resume').classList.toggle('hidden', !allPaused);
        const testingEl = document.getElementById('stat-testing');
        testingEl.textContent = `${st.counts.testing} Testing`;
        testingEl.classList.toggle('hidden', !st.counts.testing);
        const stalledEl = document.getElementById('stat-stalled');
        stalledEl.textContent = `${st.counts.stalled} Stalled`;
        stalledEl.classList.toggle('hidden', !st.counts.stalled);
        const failedEl = document.getElementById('stat-failed');
        failedEl.textContent = `${st.failed_episodes} Failed`;
        failedEl.classList.toggle('hidden', !st.failed_episodes);

        // The lists on screen are read once; reload them quietly when the ledger moved on the server.
        const fingerprint = [
          st.last_cycle_time, st.counts.works, st.counts.testing, st.counts.waiting, st.counts.upcoming, st.counts.stalled,
          st.counts.paused, st.counts.completed, st.wanted_episodes, st.completed_episodes,
          st.in_flight_episodes, st.failed_episodes, st.missed_episodes,
        ].join('|');
        const ledgerMoved = ledgerFingerprint !== null && fingerprint !== ledgerFingerprint;
        ledgerFingerprint = fingerprint;
        if (ledgerMoved) loadShows({ quiet: true });

        if (activeTab === 'history') loadHistory();
      } catch {
        // No answer (the service is restarting, or stopped): say so, and stop counting a check that may be gone.
        if (!answered) {
          serverReachable = false;
          runningCheck = null;
          clearTimeout(statusRepoll);
          statusRepoll = null;
          tickCountdown();
        }
      } finally {
        statusUpdateInFlight = false;
        if (statusUpdateQueued) {
          statusUpdateQueued = false;
          updateStatus(true);
        }
      }
    }

    // ---- First-run setup wizard ----
    // Shown on a brand-new database only. Everything the user enters is saved step by step
    // through /api/setup/*, which closes for good once the last step is done.
    const SETUP_STEPS = ['welcome', 'qbit', 'mode', 'feeds', 'downloads', 'appearance', 'anilist', 'done'];
    const SETUP_COUNTED = 6;
    const JSON_HEADERS = { 'Content-Type': 'application/json' };
    let setupOpen = false;
    let setupIndex = 0;
    let setupSandbox = false;
    let setupDraft = {};
    let setupQbitOk = false;
    let setupQbitTestId = 0;
    let setupBusy = false;
    let setupTransitioning = false;
    let setupFeedsState = 'loading';
    let setupFeedsError = '';

    const setupEl = (id) => document.getElementById(id);
    const setupStepName = () => SETUP_STEPS[setupIndex];

    // Resolves when the CSS animation `cls` on `el` has finished. The class is left on the
    // element for the caller to remove, so the end state holds until the next step is ready.
    function setupAnimate(el, cls) {
      return new Promise(resolve => {
        if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
          resolve();
          return;
        }
        let done = false;
        const finish = () => {
          if (done) return;
          done = true;
          el.removeEventListener('animationend', finish);
          resolve();
        };
        el.addEventListener('animationend', finish);
        setTimeout(finish, 300);
        el.classList.add(cls);
      });
    }

    async function openSetup(status) {
      setupOpen = true;
      setupSandbox = !!status.sandbox;
      if (setupSandbox) {
        // The label comes from the server, so a normal install never carries the text.
        const pill = document.createElement('span');
        pill.className = 'ml-auto text-xs font-medium px-2 py-0.5 rounded-full st-warn';
        pill.textContent = status.sandbox_label || 'Sandbox';
        setupEl('setup-header-title').after(pill);
      }
      let s = {};
      try {
        s = await apiFetch('/api/settings');
      } catch (err) {
        showToast(`Failed loading settings: ${err.message || err}`, 'error');
      }
      currentSettings = s;
      setupDraft = {
        download_mode: s.download_mode === 'rules' ? 'rules' : 'direct',
        accent_color: s.accent_color || DEFAULT_ACCENT,
        title_language: s.title_language === 'romaji' ? 'romaji' : 'english',
        desktop_notifications: !!s.desktop_notifications,
      };
      setupEl('setup-qbit-host').value = s.qbit_host || '';
      setupEl('setup-qbit-user').value = s.qbit_username || '';
      setupEl('setup-qbit-pass').value = '';
      setupEl('setup-base-dir').value = '';
      setupEl('setup-category').value = s.default_category || '';
      setupEl('setup-ratio').value = s.default_seed_ratio ?? 1.0;
      setupEl('setup-anilist-user').value = '';
      applyUserAccent(setupDraft.accent_color, s.accent_tint || 'subtle', { persist: false, render: false });
      setupEl('sidebar').setAttribute('inert', '');
      setupEl('main-shell').setAttribute('inert', '');
      const screen = setupEl('setup-screen');
      screen.classList.remove('hidden');
      setupShowStep(0);
      await setupAnimate(screen, 'setup-fade-in');
      screen.classList.remove('setup-fade-in');
    }

    // Swaps the visible step without animating; setupGoTo wraps this in the fade.
    function setupShowStep(index) {
      setupIndex = index;
      const name = setupStepName();
      document.querySelectorAll('#setup-stage [data-step]').forEach(el => {
        el.classList.toggle('hidden', el.dataset.step !== name);
      });
      const counted = index >= 1 && index <= SETUP_COUNTED;
      setupEl('setup-header-title').classList.toggle('hidden', name === 'welcome');
      setupEl('setup-progress').classList.toggle('hidden', !counted);
      setupEl('setup-footer').classList.toggle('hidden', !counted);
      if (counted) {
        setupEl('setup-dots').innerHTML = Array.from({ length: SETUP_COUNTED }, (_, i) => {
          const n = i + 1;
          return `<span class="setup-dot ${n <= index ? 'setup-dot-on' : ''} ${n === index ? 'setup-dot-now' : ''}"></span>`;
        }).join('');
        setupEl('setup-counter').textContent = `Step ${index} of ${SETUP_COUNTED}`;
      }
      setupEl('setup-next-label').textContent = name === 'anilist' ? 'Finish' : 'Next';
      if (name === 'mode') renderSetupMode();
      if (name === 'appearance') renderSetupAppearance();
      if (name === 'feeds') setupLoadFeeds(true);
      setupUpdateNext();
      const focusId = {
        qbit: 'setup-qbit-host', downloads: 'setup-base-dir', anilist: 'setup-anilist-user',
        mode: `setup-mode-${setupDraft.download_mode}`,
      }[name];
      const target = focusId && setupEl(focusId);
      if (target) target.focus({ preventScroll: true });
    }

    async function setupGoTo(index) {
      if (setupTransitioning) return;
      setupTransitioning = true;
      const fader = setupEl('setup-fader');
      try {
        await setupAnimate(fader, 'setup-fade-out');
        setupShowStep(index);
        fader.classList.remove('setup-fade-out');
        await setupAnimate(fader, 'setup-fade-in');
        fader.classList.remove('setup-fade-in');
      } finally {
        fader.classList.remove('setup-fade-out', 'setup-fade-in');
        setupTransitioning = false;
      }
    }

    function setupUpdateNext() {
      const name = setupStepName();
      const blocked = setupBusy
        || (name === 'qbit' && !setupQbitOk)
        || (name === 'feeds' && setupFeedsState === 'loading');
      setupEl('setup-next').disabled = blocked;
      setupEl('setup-back').disabled = setupBusy;
    }

    function setupKeydown(e) {
      if (e.key !== 'Enter' || e.isComposing) return;
      const t = e.target;
      if (t.tagName !== 'INPUT' || t.type === 'color') return;
      e.preventDefault();
      if (setupStepName() === 'qbit' && !setupQbitOk) setupTestQbit();
      else setupNext();
    }

    function setupBack() {
      if (setupBusy || setupIndex <= 0) return;
      setupGoTo(setupIndex - 1);
    }

    async function setupNext() {
      if (setupBusy || setupTransitioning) return;
      const name = setupStepName();
      if (name === 'qbit' && !setupQbitOk) return;
      if (name === 'feeds' && setupFeedsState === 'loading') return;
      await once('setup-next', async () => {
        if (name === 'anilist') {
          await setupFinish();
          return;
        }
        setupBusy = true;
        setupUpdateNext();
        try {
          if (await setupSaveStep(name)) await setupGoTo(setupIndex + 1);
        } finally {
          setupBusy = false;
          setupUpdateNext();
        }
      });
    }

    async function setupPost(url, payload) {
      return apiFetch(url, { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify(payload), notice: false });
    }

    // Stores the step on the server; false keeps the user on the step.
    async function setupSaveStep(name) {
      let payload = null;
      if (name === 'qbit') {
        payload = { qbit_host: setupEl('setup-qbit-host').value, qbit_username: setupEl('setup-qbit-user').value };
        const pwd = setupEl('setup-qbit-pass').value;
        if (pwd) payload.qbit_password = pwd;
      } else if (name === 'mode') {
        payload = { download_mode: setupDraft.download_mode };
      } else if (name === 'downloads') {
        const ratio = parseFloat(setupEl('setup-ratio').value);
        if (!Number.isFinite(ratio) || ratio < 0) {
          showToast('Enter a seed ratio of 0 or more.', 'error');
          setupEl('setup-ratio').focus();
          return false;
        }
        payload = {
          base_dir: setupEl('setup-base-dir').value,
          default_category: setupEl('setup-category').value,
          default_seed_ratio: ratio,
        };
      } else if (name === 'appearance') {
        payload = {
          accent_color: setupDraft.accent_color,
          title_language: setupDraft.title_language,
          desktop_notifications: setupDraft.desktop_notifications,
        };
      }
      if (!payload) return true;
      try {
        await setupPost('/api/setup/save', payload);
      } catch (err) {
        showToast(`Could not save: ${err.message || err}`, 'error');
        return false;
      }
      if (name === 'appearance') applyUserAccent(setupDraft.accent_color, userAccent.tint);
      return true;
    }

    function setupQbitEdited() {
      setupQbitOk = false;
      setupQbitTestId++;
      const status = setupEl('setup-qbit-status');
      status.textContent = '';
      status.className = 'text-xs font-mono';
      setupUpdateNext();
    }

    // qBittorrent's own errors run to several lines of connection-library detail, which would
    // stretch the card; the wizard shows a one-line reason instead.
    function setupQbitErrorText(err) {
      const text = String((err && err.message) || err);
      if (err instanceof TypeError) return 'Could not reach Kisetsu.';
      if (/login failed|authentic/i.test(text)) return 'Wrong username or password.';
      if (/cannot connect|connection|timed out/i.test(text)) return "Can't reach qBittorrent at that address.";
      return text.length > 80 ? `${text.slice(0, 77)}...` : text;
    }

    async function setupTestQbit() {
      const testId = ++setupQbitTestId;
      const status = setupEl('setup-qbit-status');
      status.textContent = 'Testing...';
      status.className = 'text-xs font-mono text-zinc-400';
      const payload = { qbit_host: setupEl('setup-qbit-host').value, qbit_username: setupEl('setup-qbit-user').value };
      const pwd = setupEl('setup-qbit-pass').value;
      if (pwd) payload.qbit_password = pwd;
      let ok = false;
      try {
        const data = await setupPost('/api/settings/test-qbit', payload);
        status.textContent = `Connected (qBit ${data.app_version})`;
        status.className = 'text-xs font-mono text-emerald-400 font-semibold';
        ok = true;
      } catch (err) {
        status.textContent = setupQbitErrorText(err);
        status.className = 'text-xs font-mono text-rose-400';
      }
      // The fields changed while the test ran: that answer is about older values.
      if (testId !== setupQbitTestId) return;
      setupQbitOk = ok;
      setupUpdateNext();
    }

    function renderSetupMode() {
      ['rules', 'direct'].forEach(name => {
        setupEl(`setup-mode-${name}`).setAttribute('aria-checked', String(setupDraft.download_mode === name));
      });
    }

    function setupPickMode(mode) {
      setupDraft.download_mode = mode;
      renderSetupMode();
    }

    async function setupLoadFeeds(sync) {
      setupFeedsState = 'loading';
      renderSetupFeeds();
      setupUpdateNext();
      try {
        if (sync) {
          const res = await setupPost('/api/feeds/sync', {});
          const failed = (res.logs || []).find(line => line.startsWith('Feed sync error'));
          if (failed) throw new Error(failed);
        }
        allFeeds = await apiFetch('/api/feeds');
        setupFeedsState = 'ready';
      } catch (err) {
        setupFeedsState = 'error';
        setupFeedsError = setupQbitErrorText(err);
      }
      renderSetupFeeds();
      setupUpdateNext();
    }

    function renderSetupFeeds() {
      const box = setupEl('setup-feeds-body');
      if (!setupOpen || !box) return;
      const note = (text, cls = 'text-zinc-400') => `
        <div class="p-4 space-y-3">
          <p class="text-sm ${cls}">${escapeHtml(text)}</p>
          <button type="button" onclick="setupLoadFeeds(true)" class="btn btn-sm">Check again</button>
        </div>`;
      if (setupFeedsState === 'loading') {
        box.innerHTML = '<div class="p-6 text-center text-sm text-zinc-500">Loading feeds from qBittorrent…</div>';
        return;
      }
      if (setupFeedsState === 'error') {
        box.innerHTML = note(`Could not read the feeds: ${setupFeedsError}`, 'text-rose-400');
        return;
      }
      if (!allFeeds.length) {
        box.innerHTML = `<div class="m-3 p-3 rounded-xl st-warn text-sm">qBittorrent has no RSS feeds yet. Nothing will download until you add one, in qBittorrent's RSS tab or later in Kisetsu's RSS Feeds tab.</div>
          <div class="px-3 pb-3"><button type="button" onclick="setupLoadFeeds(true)" class="btn btn-sm">Check again</button></div>`;
        return;
      }
      const arrow = (path) => `<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="${path}"/></svg>`;
      box.innerHTML = `<div class="divide-y divide-line-soft">` + allFeeds.map((f, idx) => `
        <div draggable="true" data-feed-row
             ondragstart="handleDragStart(event, ${idx})"
             ondragover="handleDragOver(event)"
             ondragleave="handleDragLeave(event)"
             ondragend="handleDragEnd(event)"
             ondrop="handleDrop(event, ${idx})"
             class="flex items-center gap-3 px-3 py-3 cursor-grab active:cursor-grabbing select-none hover:bg-raised/60 transition-colors">
          <svg class="w-4 h-4 flex-shrink-0 text-zinc-500" fill="currentColor" viewBox="0 0 24 24"><path d="M9 5a2 2 0 11-4 0 2 2 0 014 0zm0 7a2 2 0 11-4 0 2 2 0 014 0zm0 7a2 2 0 11-4 0 2 2 0 014 0zm10-14a2 2 0 11-4 0 2 2 0 014 0zm0 7a2 2 0 11-4 0 2 2 0 014 0zm0 7a2 2 0 11-4 0 2 2 0 014 0z"/></svg>
          <span class="w-7 flex-shrink-0 text-accent text-sm font-medium tabular-nums">#${f.priority}</span>
          <div class="min-w-0 flex-1">
            <div class="font-medium text-zinc-100 text-sm truncate">${escapeHtml(f.qbit_feed_name)}</div>
            <div class="text-xs text-zinc-500 font-mono truncate mt-0.5">${escapeHtml(f.qbit_feed_url)}</div>
          </div>
          <span class="flex flex-col flex-shrink-0 text-zinc-500">
            <button type="button" draggable="false" onclick="event.stopPropagation(); moveFeed(${idx}, ${idx - 1})" class="px-1 hover:text-zinc-50 disabled:opacity-20" aria-label="Move up" title="Move up" ${idx === 0 ? 'disabled' : ''}>${arrow('M5 15l7-7 7 7')}</button>
            <button type="button" draggable="false" onclick="event.stopPropagation(); moveFeed(${idx}, ${idx + 1})" class="px-1 hover:text-zinc-50 disabled:opacity-20" aria-label="Move down" title="Move down" ${idx === allFeeds.length - 1 ? 'disabled' : ''}>${arrow('M19 9l-7 7-7-7')}</button>
          </span>
        </div>`).join('') + '</div>';
    }

    function renderSetupAppearance() {
      const seg = (id, active) => { setupEl(id).className = active ? SEG_ACTIVE_CLASS : SEG_INACTIVE_CLASS; };
      const mode = themeMode();
      ['system', 'light', 'dark'].forEach(name => seg(`setup-theme-${name}`, name === mode));
      ['english', 'romaji'].forEach(name => seg(`setup-lang-${name}`, name === setupDraft.title_language));
      seg('setup-notify-on', setupDraft.desktop_notifications);
      seg('setup-notify-off', !setupDraft.desktop_notifications);
      setupEl('setup-accent-swatches').innerHTML = accentSwatchesHtml(
        setupDraft.accent_color,
        { pick: 'setupPickAccent', preview: 'setupPreviewAccent', commit: 'setupCommitAccent' },
      );
    }

    function setupSetTheme(mode) {
      setThemeMode(mode);
      renderSetupAppearance();
    }

    function setupSetLanguage(lang) {
      setupDraft.title_language = lang;
      renderSetupAppearance();
    }

    function setupSetNotifications(on) {
      setupDraft.desktop_notifications = on;
      renderSetupAppearance();
    }

    // Nothing is sent to the server until Next: the colour is only previewed here.
    function setupPickAccent(color) {
      setupDraft.accent_color = color.toLowerCase();
      applyUserAccent(setupDraft.accent_color, userAccent.tint, { persist: false, render: false });
      renderSetupAppearance();
    }

    function setupPreviewAccent(input) {
      accentPreviewing = true;
      const swatch = input.closest('label');
      if (swatch) swatch.style.background = input.value;
      clearTimeout(accentPreviewTimer);
      accentPreviewTimer = setTimeout(() => {
        applyUserAccent(input.value.toLowerCase(), userAccent.tint, { persist: false, render: false });
      }, 100);
    }

    function setupCommitAccent(color) {
      clearTimeout(accentPreviewTimer);
      accentPreviewing = false;
      setupPickAccent(color);
    }

    function setupAnilistEdited() {
      setupEl('setup-anilist-error').classList.add('hidden');
    }

    function setupFinishButton(working) {
      setupEl('setup-next-spinner').classList.toggle('hidden', !working);
      setupEl('setup-next-label').textContent = working ? 'Setting up your shows…' : 'Finish';
    }

    async function setupFinish() {
      const name = setupEl('setup-anilist-user').value.trim();
      const errEl = setupEl('setup-anilist-error');
      const fail = (text) => {
        errEl.textContent = text;
        errEl.classList.remove('hidden');
      };
      errEl.classList.add('hidden');
      if (!name) {
        fail('Enter your AniList username.');
        return;
      }
      setupBusy = true;
      setupFinishButton(true);
      setupUpdateNext();
      try {
        try {
          await setupPost('/api/setup/complete', { anilist_username: name });
        } catch (err) {
          fail(err.message || String(err));
          return;
        }
        if (setupSandbox) {
          // The sandbox must never reach the real qBittorrent, so it stops before the sync.
          setupFinishButton(false);
          await setupGoTo(SETUP_STEPS.indexOf('done'));
          return;
        }
        try {
          await apiFetch('/api/settings/sync-anilist', { method: 'POST', notice: false });
        } catch (err) {
          // Setup itself is finished; the shows arrive with the next check.
          showToast(`AniList sync failed: ${err.message || err}. Use Sync Now to retry.`, 'error');
        }
        await setupLeave();
      } finally {
        setupBusy = false;
        if (setupOpen) {
          setupFinishButton(false);
          setupUpdateNext();
        }
      }
    }

    async function setupLeave() {
      const screen = setupEl('setup-screen');
      await setupAnimate(screen, 'setup-fade-out');
      screen.classList.add('hidden');
      screen.classList.remove('setup-fade-out');
      setupOpen = false;
      applyUserAccent(setupDraft.accent_color, userAccent.tint);
      const shell = [setupEl('sidebar'), setupEl('main-shell')];
      shell.forEach(el => el.removeAttribute('inert'));
      startApp();
      for (const el of shell) {
        setupAnimate(el, 'setup-fade-in').then(() => el.classList.remove('setup-fade-in'));
      }
    }

    async function setupStartOver() {
      try {
        await setupPost('/api/setup/reset', {});
        location.reload();
      } catch (err) {
        showToast(`Could not start over: ${err.message || err}`, 'error');
      }
    }

    async function boot() {
      applyTheme();
      let status = null;
      try {
        status = await apiFetch('/api/setup/status');
      } catch (err) {
        // Never lock the user out: without an answer the app opens as it always did.
      }
      if (status && !status.setup_completed) openSetup(status);
      else startApp();
    }

    // A required field inside the closed Advanced block would block saving without anything showing why.
    document.getElementById('settings-form').addEventListener('invalid', e => {
      const block = e.target.closest('details');
      if (block) block.open = true;
    }, true);

    new ResizeObserver(fitShowGrids).observe(document.getElementById('main-scroll-container'));
    window.addEventListener('resize', fitShowGrids);

    // Starts the app proper: the first-run wizard calls this when it is done, otherwise it runs at once.
    let appStarted = false;
    function startApp() {
      if (appStarted) return;
      appStarted = true;
      loadShows();
      loadHistory();
      updateStatus();
      if (showIdFromHash() !== null) openShowPage(showIdFromHash(), { push: false, instant: true });
      setInterval(updateStatus, 15000);
      setInterval(tickCountdown, 30000);
      setInterval(() => { if (!document.hidden) tickNextCheck(); }, 1000);
      toggleLogsAutoRefresh(true);
    }

    document.addEventListener('visibilitychange', () => {
      if (!document.hidden && appStarted) updateStatus();
    });

    document.addEventListener('keydown', (e) => {
      if (e.key !== 'Escape' || currentInspectedShowId === null) return;
      // Escape in an open dialog closes only the dialog.
      const dialogOpen = [...document.querySelectorAll('[aria-modal="true"]')].some(el => !el.closest('.hidden'));
      if (!dialogOpen) closeShowPage();
    });

    // Browser back/forward moves between the list and a show page.
    window.addEventListener('popstate', () => {
      if (ignoreNextPop) {
        ignoreNextPop = false;
        return;
      }
      const id = showIdFromHash();
      if (id !== null) {
        if (id !== currentInspectedShowId) openShowPage(id, { push: false, fromHistory: true });
      } else if (currentInspectedShowId !== null) {
        if (!confirmDiscardShowChanges()) {
          history.pushState({ show: currentInspectedShowId }, '', `#show/${currentInspectedShowId}`);
          return;
        }
        showInitialState = null;
        leaveShowPage();
      }
    });

    // Sliding pill behind the active option of every .seg control.
    const segResizeObserver = new ResizeObserver(entries => entries.forEach(e => syncSegPill(e.target)));
    const segObserved = new WeakSet();

    function syncSegPill(seg) {
      let pill = seg.querySelector(':scope > .seg-pill');
      if (!pill) {
        pill = document.createElement('span');
        pill.className = 'seg-pill';
        seg.prepend(pill);
      }
      const active = seg.querySelector(':scope > button.seg-on');
      if (!active || !active.offsetWidth) {
        // Hidden or nothing selected: fall back to the button's own background and skip the
        // slide the next time it is placed.
        seg.classList.remove('seg-ready');
        pill.classList.remove('seg-pill-anim');
        return;
      }
      pill.style.width = `${active.offsetWidth}px`;
      pill.style.height = `${active.offsetHeight}px`;
      pill.style.transform = `translate(${active.offsetLeft}px, ${active.offsetTop}px)`;
      if (!seg.classList.contains('seg-ready')) {
        seg.classList.add('seg-ready');
        pill.getBoundingClientRect();
        pill.classList.add('seg-pill-anim');
      }
    }

    function syncAllSegPills() {
      document.querySelectorAll('.seg').forEach(seg => {
        if (!segObserved.has(seg)) { segObserved.add(seg); segResizeObserver.observe(seg); }
        syncSegPill(seg);
      });
    }

    let segSyncQueued = false;
    new MutationObserver(() => {
      if (segSyncQueued) return;
      segSyncQueued = true;
      requestAnimationFrame(() => { segSyncQueued = false; syncAllSegPills(); });
    }).observe(document.body, { subtree: true, childList: true, attributes: true, attributeFilter: ['class'] });
    syncAllSegPills();

    boot();
