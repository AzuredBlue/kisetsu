from typing import Dict, Optional

from fastapi.responses import HTMLResponse

def get_web_ui_html(headers: Optional[Dict[str, str]] = None) -> HTMLResponse:
    html = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Kisetsu</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
  <script src="https://cdn.tailwindcss.com"></script>
  <script>
    tailwind.config = {
      theme: {
        extend: {
          fontFamily: {
            sans: ['Inter', 'ui-sans-serif', 'system-ui', '-apple-system', 'Segoe UI', 'Roboto', 'sans-serif'],
          },
          colors: {
            canvas: 'rgb(var(--bg) / <alpha-value>)',
            sunken: 'rgb(var(--sunken) / <alpha-value>)',
            chrome: 'rgb(var(--chrome) / <alpha-value>)',
            surface: 'rgb(var(--surface) / <alpha-value>)',
            raised: 'rgb(var(--raised) / <alpha-value>)',
            raised2: 'rgb(var(--raised2) / <alpha-value>)',
            'line-soft': 'rgb(var(--line-soft) / <alpha-value>)',
            line: 'rgb(var(--line) / <alpha-value>)',
            'line-strong': 'rgb(var(--line-strong) / <alpha-value>)',
            accent: { DEFAULT: 'rgb(var(--ac) / <alpha-value>)', strong: 'rgb(var(--ac-strong) / <alpha-value>)', soft: 'rgb(var(--ac-soft) / <alpha-value>)' },
            accent2: 'rgb(var(--ac2) / <alpha-value>)',
          },
          borderRadius: { xl: '0.875rem', '2xl': '1.125rem' },
        },
      },
    };
  </script>
  <style>
    * { scrollbar-width: thin; scrollbar-color: rgb(var(--line)) transparent; }
    :root {
      --ac: 45 212 191; --ac-soft: 94 234 212; --ac-strong: 20 184 166; --ac-ink: 4 32 30; --ac2: 56 189 248;
      --bg: 19 19 23; --sunken: 15 15 19; --chrome: 23 23 28; --surface: 27 27 33; --field: 19 19 23; --raised: 35 35 43; --raised2: 43 43 53; --line: 47 47 58; --line-soft: 37 37 45; --line-hover: 58 58 72; --line-strong: 74 74 90;
    }
    ::-webkit-scrollbar { width: 8px; height: 8px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: rgb(var(--line)); border-radius: 4px; }
    ::-webkit-scrollbar-thumb:hover { background: rgb(var(--line-strong)); }

    .line-clamp-2 {
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }

    /* Shared form and button vocabulary, so markup stays short. */
    .field-label { display: block; font-size: 0.8125rem; font-weight: 500; color: #d4d4d8; margin-bottom: 0.375rem; }
    .field-hint { font-size: 0.75rem; line-height: 1.45; color: #8b8b97; margin-top: 0.375rem; }
    .field {
      width: 100%; background: rgb(var(--field)); border: 1px solid rgb(var(--line)); border-radius: 0.625rem;
      padding: 0.55rem 0.8rem; font-size: 0.875rem; color: #f4f4f5; outline: none;
      transition: border-color .15s, box-shadow .15s;
    }
    .field:focus { border-color: rgb(var(--ac)); }
    .field::placeholder { color: #5d5d6b; }
    .btn {
      display: inline-flex; align-items: center; justify-content: center; gap: 0.4rem;
      font-size: 0.8125rem; font-weight: 500; padding: 0.5rem 0.9rem; border-radius: 0.625rem;
      background: rgb(var(--raised)); color: #e4e4e7; border: 1px solid rgb(var(--line));
      transition: background-color .15s, border-color .15s, color .15s, transform .1s; cursor: pointer;
    }
    .btn:hover { background: rgb(var(--raised2)); border-color: rgb(var(--line-hover)); }
    .btn:active { transform: scale(.97); }
    .btn:focus-visible, .nav-item:focus-visible { outline: 2px solid rgb(var(--ac)); outline-offset: 2px; }
    .btn-primary { background: rgb(var(--ac-strong)); border-color: rgb(var(--ac-strong)); color: rgb(var(--ac-ink)); font-weight: 600; }
    .btn-primary:hover { background: rgb(var(--ac)); border-color: rgb(var(--ac)); }
    #btn-run-cycle:hover { border-color: rgb(var(--ac)); color: rgb(var(--ac-soft)); }
    .nav-marker { box-shadow: inset 2px 0 0 0 rgb(var(--ac)); }
    .btn-danger { background: #2a1818; border-color: #4a2424; color: #f0868b; }
    .btn-danger:hover { background: #381d1d; border-color: #5e2b2b; }
    .btn-sm { padding: 0.35rem 0.7rem; font-size: 0.75rem; }
    .card { background: rgb(var(--surface)); border: 1px solid rgb(var(--line)); border-radius: 0.875rem; }
    .page-title { font-size: 1.125rem; font-weight: 600; color: #fafafa; letter-spacing: -0.01em; }
    .page-sub { font-size: 0.8125rem; color: #8b8b97; margin-top: 0.2rem; }
    .section-title { font-size: 0.8125rem; font-weight: 600; color: #d4d4d8; letter-spacing: 0.02em; }
    .seg { display: inline-flex; background: rgb(var(--field)); border: 1px solid rgb(var(--line)); border-radius: 0.625rem; padding: 2px; }
    .seg > button { padding: 0.3rem 0.8rem; font-size: 0.8125rem; border-radius: 0.5rem; color: #9a9aa6; transition: background-color .15s, color .15s; }
    .seg > button:hover { color: #e4e4e7; }

    /* Show cards: status chips are dots that grow into their label on hover, and the
       pause/delete buttons fade in, both with the same timing. */
    .show-grid { display: grid; gap: 1.25rem; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); }
    @media (min-width: 1024px) { .show-grid { grid-template-columns: repeat(auto-fill, minmax(215px, 1fr)); } }
    /* The poster zoom grows the layer's box instead of using a transform. A transform is
       rasterised as its own layer, and its clipped edge landed on a sub-pixel (card
       heights are fractional) and showed as a seam. -2% on every side is a ~1.04 zoom. */
    .poster-zoom { transition: inset .3s ease-out; }
    .group:hover .poster-zoom { inset: -2%; }
    .status-chip { position: relative; }
    .status-chip::before { content: ''; position: absolute; inset: -10px; }
    .status-chip .status-label { max-width: 0; opacity: 0; overflow: hidden; white-space: nowrap; margin-left: 0; transition: max-width .2s ease-out, opacity .2s ease-out, margin .2s ease-out; }
    .status-chip:hover .status-label, .status-chip:focus-visible .status-label { max-width: 5rem; opacity: 1; margin-left: 0.375rem; }
    .card-actions { opacity: 0; pointer-events: none; transition: opacity .2s ease-out; }
    .group:hover .card-actions, .group:focus-within .card-actions { opacity: 1; pointer-events: auto; }

    #tab-show { background-color: rgb(var(--bg)); }
    body.show-open #sidebar .btn:hover { border-color: rgb(var(--ac) / .7); color: rgb(var(--ac-soft)); }
    #tab-show .card { background-color: rgb(var(--surface) / .85); border-color: rgb(var(--line)); backdrop-filter: blur(10px); }
    #tab-show .field, #tab-show .seg { background-color: rgb(var(--field)); border-color: rgb(var(--line)); }
    #tab-show .field:focus { border-color: rgb(var(--ac)); box-shadow: 0 0 0 3px rgb(var(--ac) / .2); }
    #tab-show .bg-canvas { background-color: rgb(var(--field) / .85); }
    #tab-show .border-line { border-color: rgb(var(--line)); }
    #tab-show .divide-line-soft > :not([hidden]) ~ :not([hidden]) { border-color: rgb(var(--line-soft)); }
        #tab-show .btn:not(.btn-primary):not(.btn-danger):hover { border-color: rgb(var(--ac) / .7); color: rgb(var(--ac-soft)); }
    #tab-show .section-title { color: rgb(var(--ac-soft)); }
    #tab-show #show-save-bar { border-color: rgb(var(--ac) / .5); background-color: rgb(var(--surface) / .95); }
    #show-backdrop-art { -webkit-mask-image: linear-gradient(to bottom, #000 0, rgba(0, 0, 0, .5) 45%, transparent 90%); mask-image: linear-gradient(to bottom, #000 0, rgba(0, 0, 0, .5) 45%, transparent 90%); }
    #show-banner-art { -webkit-mask-image: linear-gradient(to bottom, #000 60%, transparent 100%); mask-image: linear-gradient(to bottom, #000 60%, transparent 100%); }
    #tab-show ::selection { background: rgb(var(--ac) / .35); color: #fff; }
    #tab-show * { scrollbar-color: rgb(var(--ac) / .45) transparent; }
    #tab-show ::-webkit-scrollbar-thumb { background: rgb(var(--ac) / .45); }
    @keyframes vt-rise { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: none; } }
    @keyframes vt-drop { from { opacity: 1; transform: none; } to { opacity: 0; transform: translateY(8px); } }
    @keyframes vt-fade-in { from { opacity: 0; } to { opacity: 1; } }
    @keyframes vt-fade-out { from { opacity: 1; } to { opacity: 0; } }
    .page-enter { animation: vt-rise .26s ease-out; }
    #sidebar { view-transition-name: sidebar; }
    #main-shell > header { view-transition-name: topbar; }
    #main-scroll-container, #tab-show { view-transition-name: content; }
    #toast-container { view-transition-name: toasts; }
    ::view-transition-group(toasts) { animation: none; }
    ::view-transition-old(toasts) { display: none; }
    ::view-transition-new(toasts) { animation: none; }
    html.vt-open::view-transition-old(content) { animation: vt-fade-out .28s ease-out both; }
    html.vt-open::view-transition-new(content) { animation: vt-rise .28s ease-out both; }
    html.vt-close::view-transition-old(content) { animation: vt-drop .28s ease-out both; }
    html.vt-close::view-transition-new(content) { animation: vt-fade-in .28s ease-out both; }
    ::view-transition-group(show-poster) { animation-duration: .32s; animation-timing-function: cubic-bezier(.2, .8, .2, 1); }

    @media (prefers-reduced-motion: reduce) {
      ::view-transition-group(*), ::view-transition-old(*), ::view-transition-new(*) { animation: none !important; }
      .page-enter { animation: none; }
      .group:hover { transform: none !important; }
      .group:hover .poster-zoom { inset: 0; }
    }

    @media (hover: none) {
      .card-actions { opacity: 1 !important; pointer-events: auto !important; }
    }
  </style>
  <script>
    try {
      var cachedAccent = localStorage.getItem('kisetsu_accent_css');
      if (cachedAccent) {
        var accentStyle = document.createElement('style');
        accentStyle.id = 'user-accent';
        accentStyle.textContent = cachedAccent;
        document.head.appendChild(accentStyle);
      }
    } catch (err) {}
  </script>
</head>
<body class="bg-canvas text-zinc-100 flex h-screen overflow-hidden font-sans antialiased selection:bg-zinc-600 selection:text-white">

  <div id="sidebar-backdrop" onclick="toggleSidebar(false)" class="fixed inset-0 bg-black/60 z-30 hidden md:hidden"></div>

  <aside id="sidebar" class="fixed md:static inset-y-0 left-0 w-52 -translate-x-full md:translate-x-0 transition-transform duration-200 bg-chrome flex flex-col flex-shrink-0 select-none z-40">

    <div class="px-4 pt-4 pb-2 flex items-center">
      <span class="text-sm font-semibold text-zinc-100">Kisetsu</span>
    </div>

    <nav class="flex-1 px-2 py-1 space-y-0.5 overflow-y-auto">
      <button onclick="switchTab('shows')" id="nav-shows" class="nav-item w-full flex items-center gap-2.5 px-2.5 py-1.5 rounded-md text-sm font-medium transition-colors bg-raised2 text-white nav-marker [&>svg]:text-accent">
        <svg class="w-4 h-4 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="1.8"><path stroke-linecap="round" stroke-linejoin="round" d="M7 4v16M17 4v16M3 8h4m10 0h4M3 12h18M3 16h4m10 0h4M4 20h16a1 1 0 001-1V5a1 1 0 00-1-1H4a1 1 0 00-1 1v14a1 1 0 001 1z"/></svg>
        <span>Shows</span>
        <span id="badge-total-shows" class="ml-auto text-xs text-zinc-500 tabular-nums font-medium">0</span>
      </button>

      <button onclick="switchTab('calendar')" id="nav-calendar" class="nav-item w-full flex items-center gap-2.5 px-2.5 py-1.5 rounded-md text-sm font-medium transition-colors text-zinc-400 hover:text-zinc-100 hover:bg-raised">
        <svg class="w-4 h-4 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="1.8"><path stroke-linecap="round" stroke-linejoin="round" d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z"/></svg>
        <span>Calendar</span>
        <span id="badge-calendar-shows" class="ml-auto text-xs text-zinc-500 tabular-nums font-medium">0</span>
      </button>

      <button onclick="switchTab('history')" id="nav-history" class="nav-item w-full flex items-center gap-2.5 px-2.5 py-1.5 rounded-md text-sm font-medium transition-colors text-zinc-400 hover:text-zinc-100 hover:bg-raised">
        <svg class="w-4 h-4 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="1.8"><path stroke-linecap="round" stroke-linejoin="round" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
        <span>History</span>
        <span id="badge-total-history" class="ml-auto text-xs text-zinc-500 tabular-nums font-medium">0</span>
      </button>

      <button onclick="switchTab('feeds')" id="nav-feeds" class="nav-item w-full flex items-center gap-2.5 px-2.5 py-1.5 rounded-md text-sm font-medium transition-colors text-zinc-400 hover:text-zinc-100 hover:bg-raised">
        <svg class="w-4 h-4 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="1.8"><path stroke-linecap="round" stroke-linejoin="round" d="M6 5c7.18 0 13 5.82 13 13M6 11a7 7 0 017 7m-6 0a1 1 0 11-2 0 1 1 0 012 0z"/></svg>
        <span>RSS Feeds</span>
      </button>

      <button onclick="switchTab('logs')" id="nav-logs" class="nav-item w-full flex items-center gap-2.5 px-2.5 py-1.5 rounded-md text-sm font-medium transition-colors text-zinc-400 hover:text-zinc-100 hover:bg-raised">
        <svg class="w-4 h-4 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="1.8"><path stroke-linecap="round" stroke-linejoin="round" d="M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z"/></svg>
        <span>Logs</span>
      </button>

      <button onclick="switchTab('settings')" id="nav-settings" class="nav-item w-full flex items-center gap-2.5 px-2.5 py-1.5 rounded-md text-sm font-medium transition-colors text-zinc-400 hover:text-zinc-100 hover:bg-raised">
        <svg class="w-4 h-4 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="1.8"><path stroke-linecap="round" stroke-linejoin="round" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"/><path stroke-linecap="round" stroke-linejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/></svg>
        <span>Settings</span>
      </button>
    </nav>

    <div class="p-3 space-y-2">
      <div class="flex items-baseline justify-between gap-2 px-0.5">
        <span class="text-[11px] text-zinc-500">Next check</span>
        <span id="sidebar-next-check" class="text-zinc-300 text-xs tabular-nums" title="">Calculating...</span>
      </div>

      <button onclick="runCycleNow()" id="btn-run-cycle" title="Re-sync AniList, refresh RSS feeds, grab or confirm new episodes and reconcile with qBittorrent" class="btn btn-sm w-full">
        <svg id="spinner-run-cycle" class="w-4 h-4 hidden animate-spin text-zinc-400" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z"></path></svg>
        <span id="text-run-cycle">Sync Now</span>
      </button>
    </div>
  </aside>

  <main id="main-shell" class="flex-1 flex flex-col min-w-0 bg-canvas overflow-hidden">

    <header class="h-14 px-4 md:px-8 flex items-center justify-between gap-3 flex-shrink-0 bg-chrome">
      <div class="flex items-center gap-3 min-w-0">
        <button onclick="toggleSidebar(true)" class="md:hidden p-1.5 -ml-1.5 rounded-lg text-zinc-300 hover:bg-raised" aria-label="Open menu">
          <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M4 6h16M4 12h16M4 18h16"/></svg>
        </button>
        <h1 id="page-title" class="text-sm font-semibold text-zinc-100 truncate">Shows</h1>
      </div>

      <div class="flex items-center gap-3 text-xs font-medium flex-shrink-0">
        <span id="stat-working" class="text-emerald-400">0 Working</span>
        <span id="stat-testing" class="text-amber-400 hidden">0 Testing</span>
        <span id="stat-upcoming" class="text-sky-400">0 Upcoming</span>
        <span id="stat-stalled" class="text-rose-400 hidden">0 Stalled</span>
      </div>
    </header>

    <div class="flex-1 overflow-y-auto px-4 pt-5 pb-5 md:px-8 md:pt-5 md:pb-7" id="main-scroll-container">

      <section id="tab-shows" class="space-y-5 max-w-[1900px]">

        <div id="section-releasing" class="space-y-4">
          <div class="flex items-center justify-between gap-3">
            <div class="flex items-baseline gap-2">
              <span class="w-1.5 h-1.5 rounded-full bg-accent self-center"></span>
              <h2 class="section-title">Releasing</h2>
              <span id="header-count-releasing" class="text-xs text-zinc-500 tabular-nums">(0)</span>
            </div>
            <div class="flex items-center gap-0.5 bg-sunken p-0.5 rounded-lg border border-line-soft text-xs">
              <button onclick="setSortMode('airing')" id="sort-btn-airing" class="px-2.5 py-1 rounded-md flex items-center gap-1.5 transition-colors text-zinc-400 hover:text-zinc-200" title="Sort by Next Episode Airing (Soonest first)">
                <svg class="w-3.5 h-3.5 text-zinc-400" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
                <span>Airing</span>
              </button>
              <button onclick="setSortMode('title')" id="sort-btn-title" class="px-2.5 py-1 rounded-md flex items-center gap-1.5 transition-colors text-zinc-400 hover:text-zinc-200" title="Sort Alphabetically (A to Z)">
                <svg class="w-3.5 h-3.5 text-zinc-400" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M3 4h13M3 8h9m-9 4h6m4 0l4-4m0 0l4 4m-4-4v12"/></svg>
                <span>A-Z</span>
              </button>
              <button onclick="setSortMode('default')" id="sort-btn-default" class="px-2.5 py-1 rounded-md flex items-center gap-1.5 transition-colors text-zinc-400 hover:text-zinc-200" title="Default Order (Date added)">
                <svg class="w-3.5 h-3.5 text-zinc-400" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M4 6h16M4 10h16M4 14h16M4 18h16"/></svg>
                <span>Default</span>
              </button>
            </div>
          </div>
          <div id="grid-releasing" class="show-grid">
          </div>
        </div>

        <div id="section-completed" class="space-y-4">
          <div class="flex items-center justify-between gap-3">
            <div class="flex items-baseline gap-2">
              <span class="w-1.5 h-1.5 rounded-full bg-violet-400 self-center"></span>
              <h2 class="section-title">Completed</h2>
              <span id="header-count-completed" class="text-xs text-zinc-500 tabular-nums">(0)</span>
            </div>
          </div>
          <div id="grid-completed" class="show-grid">
          </div>
        </div>

        <div id="section-planned" class="space-y-4">
          <div class="flex items-center justify-between gap-3">
            <div class="flex items-baseline gap-2">
              <span class="w-1.5 h-1.5 rounded-full bg-sky-400 self-center"></span>
              <h2 class="section-title">Planned</h2>
              <span id="header-count-planned" class="text-xs text-zinc-500 tabular-nums">(0)</span>
            </div>
          </div>
          <div id="grid-planned" class="show-grid">
          </div>
        </div>

      </section>

      <section id="tab-calendar" class="space-y-5 max-w-[1950px] hidden">

        <div>
          <p id="calendar-week-range" class="text-sm text-zinc-400"></p>
        </div>

        <div id="calendar-weekly-grid" class="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 xl:grid-cols-7 gap-4 items-start select-none">
        </div>

      </section>

      <section id="tab-history" class="space-y-5 max-w-4xl hidden">
        <div class="flex items-center justify-between gap-3">
          <p class="page-sub !mt-0">Releases added or matched for your shows.</p>

          <div class="flex items-center gap-2 flex-shrink-0">
            <button onclick="loadHistory(true)" class="btn btn-sm">
              <svg class="w-3.5 h-3.5 text-zinc-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg>
              <span>Refresh</span>
            </button>
            <button onclick="clearHistory()" class="btn btn-sm hover:!text-rose-400">
              Clear
            </button>
          </div>
        </div>

        <div id="history-container" class="space-y-2"></div>
      </section>

      <section id="tab-feeds" class="hidden max-w-4xl space-y-5">
        <div class="flex items-center justify-between gap-3">
          <p class="page-sub !mt-0">Prioritized torrent indexer feeds queried during supervision cycles. Drag rows to reorder.</p>
          <button onclick="syncFeeds()" class="btn btn-sm flex-shrink-0">
            Sync from qBittorrent
          </button>
        </div>

        <div class="card overflow-hidden">
          <div class="overflow-x-auto">
            <table class="w-full text-left text-sm min-w-[480px]">
              <thead class="bg-chrome text-zinc-500 border-b border-line-soft text-xs font-medium">
                <tr>
                  <th class="py-3 px-3 w-10 text-center"></th>
                  <th class="py-3 px-3 w-24">Priority</th>
                  <th class="py-3 px-4">Feed name &amp; RSS URL</th>
                </tr>
              </thead>
              <tbody id="feeds-table-body" class="divide-y divide-line-soft">
              </tbody>
            </table>
          </div>
        </div>
      </section>

      <section id="tab-settings" class="hidden max-w-3xl space-y-5">

        <form id="settings-form" onsubmit="saveSettings(event)" class="space-y-5">

          <div>
            <h3 class="section-title mb-2 px-1 flex items-center gap-2"><span class="w-1.5 h-1.5 rounded-full bg-accent"></span>qBittorrent</h3>
            <div class="card p-4 grid grid-cols-1 sm:grid-cols-2 gap-x-4 gap-y-3">
              <div class="sm:col-span-2">
                <label class="field-label" for="set-qbit-host">Host / URL</label>
                <input id="set-qbit-host" type="text" required class="field">
              </div>
              <div>
                <label class="field-label" for="set-qbit-user">Username</label>
                <input id="set-qbit-user" type="text" required class="field">
              </div>
              <div>
                <label class="field-label" for="set-qbit-pass">Password</label>
                <input id="set-qbit-pass" type="password" placeholder="Unchanged" class="field">
              </div>
              <div class="sm:col-span-2 flex items-center gap-3">
                <button type="button" onclick="testQbitConnection()" class="btn btn-sm">Test connection</button>
                <span id="test-qbit-status" class="text-xs font-mono"></span>
              </div>
            </div>
          </div>

          <div>
            <h3 class="section-title mb-2 px-1 flex items-center gap-2"><span class="w-1.5 h-1.5 rounded-full bg-sky-400"></span>Downloads</h3>
            <div class="card p-4 grid grid-cols-1 sm:grid-cols-3 gap-x-4 gap-y-3">
              <div class="sm:col-span-3">
                <label class="field-label" for="set-base-dir" title="{name} becomes the show name. Blank uses qBittorrent's default.">Base directory</label>
                <input id="set-base-dir" type="text" placeholder="~/Anime/{name}" class="field font-mono">
              </div>
              <div>
                <label class="field-label" for="set-category">Category</label>
                <input id="set-category" type="text" placeholder="(blank)" class="field">
              </div>
              <div>
                <label class="field-label" for="set-ratio">Seed ratio</label>
                <input id="set-ratio" type="number" step="0.1" min="0" required class="field">
              </div>
              <div>
                <label class="field-label" for="set-stall-window" title="Idle time before a download counts as stalled.">Stall grace (h)</label>
                <input id="set-stall-window" type="number" min="1" required class="field">
              </div>
            </div>
          </div>

          <div>
            <h3 class="section-title mb-2 px-1 flex items-center gap-2"><span class="w-1.5 h-1.5 rounded-full bg-amber-400"></span>AniList &amp; schedule</h3>
            <div class="card p-4 grid grid-cols-1 sm:grid-cols-2 gap-x-4 gap-y-3">
              <div>
                <label class="field-label" for="set-anilist-user" title="Whose watching list to follow.">AniList username</label>
                <input id="set-anilist-user" type="text" class="field">
              </div>
              <div>
                <label class="field-label" for="set-interval" title="How often to sync AniList and check feeds.">Check every (min)</label>
                <input id="set-interval" type="number" min="5" required class="field">
              </div>
              <div>
                <label class="field-label" for="set-early-air-tolerance" title="For a show's first episodes only: start looking this long before AniList's air time. 0 waits for the stated time.">Early air (h)</label>
                <input id="set-early-air-tolerance" type="number" min="0" max="168" required class="field">
              </div>
            </div>
          </div>

          <div>
            <h3 class="section-title mb-2 px-1 flex items-center gap-2"><span class="w-1.5 h-1.5 rounded-full bg-violet-400"></span>Appearance &amp; behaviour</h3>
            <div class="card p-4 grid grid-cols-1 sm:grid-cols-2 gap-x-4 gap-y-4">
              <div>
                <div class="field-label">Anime names</div>
                <div class="seg">
                  <button type="button" onclick="setTitleLanguage('english')" id="btn-lang-en" class="font-semibold bg-accent/15 text-accent-soft">English</button>
                  <button type="button" onclick="setTitleLanguage('romaji')" id="btn-lang-ja" class="font-medium text-zinc-400 hover:text-zinc-200">Romaji</button>
                </div>
                <input type="hidden" id="set-title-language" value="english">
              </div>
              <div>
                <div class="field-label" title="Rules lets qBittorrent's RSS rules download. Direct adds torrents itself and handles v2 replacements.">Download engine</div>
                <div class="seg">
                  <button type="button" onclick="setDownloadMode('rules')" id="btn-mode-rules" class="font-semibold bg-accent/15 text-accent-soft">Rules</button>
                  <button type="button" onclick="setDownloadMode('direct')" id="btn-mode-direct" class="font-medium text-zinc-400 hover:text-zinc-200">Direct</button>
                </div>
                <input type="hidden" id="set-download-mode" value="rules">
              </div>
              <div>
                <div class="field-label">Accent colour</div>
                <div id="accent-swatches" class="flex flex-wrap items-center gap-2 py-1"></div>
              </div>
              <div>
                <div class="field-label" title="How much the accent colours the backgrounds.">Colour tint</div>
                <div class="seg">
                  <button type="button" onclick="setAccentTint('off')" id="btn-tint-off">Off</button>
                  <button type="button" onclick="setAccentTint('subtle')" id="btn-tint-subtle">Subtle</button>
                  <button type="button" onclick="setAccentTint('full')" id="btn-tint-full">Full</button>
                </div>
              </div>
            </div>
          </div>

          <div class="flex justify-end">
            <button type="submit" class="btn btn-primary px-6 py-2">
              Save settings
            </button>
          </div>
        </form>

        <div class="card px-4 py-3 flex items-center justify-between gap-4">
          <div>
            <h3 class="section-title">Clear all shows</h3>
            <p class="field-hint !mt-0.5">Removes every show, the history and the app's RSS rules. Downloads are kept.</p>
          </div>
          <button type="button" onclick="clearAllShows()" class="btn btn-danger btn-sm flex-shrink-0">
            Clear all
          </button>
        </div>
      </section>

      <section id="tab-logs" class="space-y-4 max-w-6xl hidden">
        <div class="flex items-center justify-between gap-3 flex-wrap">
          <p class="page-sub !mt-0">Live trace of supervisor cycles, AniList updates, RSS discoveries and state changes.</p>

          <div class="flex items-center gap-2">
            <label class="btn btn-sm cursor-pointer select-none">
              <input type="checkbox" id="logs-auto-refresh" checked onchange="toggleLogsAutoRefresh(this.checked)" class="rounded bg-canvas border-line text-zinc-300 focus:ring-0">
              <span>Auto-refresh</span>
            </label>
            <button onclick="loadLogs(true)" class="btn btn-sm">
              <svg class="w-3.5 h-3.5 text-zinc-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg>
              <span>Refresh</span>
            </button>
            <button onclick="copyLogs()" class="btn btn-sm">
              <svg class="w-3.5 h-3.5 text-zinc-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z"/></svg>
              <span>Copy</span>
            </button>
          </div>
        </div>

        <div class="bg-sunken border border-line-soft rounded-xl overflow-hidden flex flex-col h-[calc(100dvh-13rem)] min-h-[320px]">
          <div class="h-10 bg-chrome border-b border-line-soft px-4 flex items-center justify-between flex-shrink-0 text-xs text-zinc-400">
            <div class="flex items-center gap-2">
              <span class="text-zinc-300 font-medium">Activity stream</span>
            </div>
            <div id="logs-count-badge" class="text-zinc-500 text-[11px]">0 entries</div>
          </div>

          <div id="logs-container" class="flex-1 p-4 font-mono text-xs overflow-y-auto space-y-1.5 bg-sunken select-text">
            <div class="text-zinc-600 text-center py-12">Loading supervisor logs...</div>
          </div>
        </div>
      </section>

    </div>

  <section id="tab-show" class="hidden flex-1 min-h-0 flex flex-col overflow-y-auto lg:overflow-hidden relative isolate">

      <div id="show-backdrop" class="absolute inset-x-0 top-0 h-full -z-10 pointer-events-none overflow-hidden">
        <div id="show-backdrop-art" class="absolute inset-0"></div>
        <div class="absolute inset-0" style="background: rgb(var(--bg) / .5)"></div>
      </div>

      <div class="relative flex-shrink-0 h-[clamp(15rem,34vh,24rem)] overflow-hidden">
        <div id="show-banner-art" class="absolute inset-0"></div>
        <div class="absolute inset-x-0 top-0 h-20 bg-gradient-to-b from-black/30 to-transparent"></div>
        <button type="button" onclick="closeShowPage()" class="absolute top-3 left-4 md:top-4 md:left-8 w-8 h-8 rounded-md flex items-center justify-center bg-black/50 hover:bg-black/70 backdrop-blur text-zinc-100 transition-colors active:scale-90" aria-label="Back" title="Back (Esc)">
          <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2.2"><path stroke-linecap="round" stroke-linejoin="round" d="M15 19l-7-7 7-7"/></svg>
        </button>
      </div>

      <div id="show-body" oninput="updateSaveBar()" onchange="updateSaveBar()" class="flex-1 lg:min-h-0 grid grid-cols-1 lg:grid-cols-[minmax(0,29rem)_minmax(0,1fr)_17rem] lg:grid-rows-[auto_minmax(0,1fr)] gap-x-6 gap-y-5 px-4 md:px-8 pb-6 max-w-[1700px] w-full">

        <div class="order-1 lg:order-none lg:col-span-3 flex flex-col lg:flex-row lg:items-end gap-x-6 gap-y-4 min-w-0">
          <div id="show-poster" class="relative z-10 -mt-24 lg:-mt-40 w-36 lg:w-[13.5rem] aspect-[2/3] flex-shrink-0 rounded-lg overflow-hidden bg-surface border border-line shadow-lg shadow-black/40"></div>
          <div id="show-title-block" class="min-w-0 flex-1 lg:pb-1"></div>
        </div>

        <div id="show-side-form" class="order-3 lg:order-none min-w-0 lg:min-h-0 flex flex-col"></div>
        <div id="show-lists" class="order-2 lg:order-none flex flex-col lg:min-h-0 min-w-0"></div>
        <div id="show-col-feed" class="order-4 lg:order-none min-w-0 lg:min-h-0 flex flex-col"></div>

      </div>

      <div id="show-save-bar" inert aria-hidden="true" class="absolute bottom-5 left-1/2 -translate-x-1/2 z-30 card shadow-xl shadow-black/50 pl-4 pr-2.5 py-2.5 flex items-center gap-4 opacity-0 translate-y-3 pointer-events-none transition-[transform,opacity] duration-200 ease-out motion-reduce:transition-none">
        <span class="text-sm text-zinc-300 whitespace-nowrap">Unsaved changes</span>
        <div class="flex items-center gap-2">
          <button type="button" onclick="discardShowChanges()" class="btn btn-sm">Discard</button>
          <button type="button" id="btn-save-show" onclick="saveShowPage()" class="btn btn-sm btn-primary">Save changes</button>
        </div>
      </div>

    </section>
  </main>

  <div id="toast-container" class="fixed bottom-4 right-4 z-50 flex flex-col gap-2 max-w-xs"></div>

  <script>
    let allShows = [];
    let allFeeds = [];
    let currentSettings = {};
    let activeTab = 'shows';
    let currentInspectedShowId = null;

    // Muted status colours: dim text on near-card-tone backgrounds, so a tag reads as
    // a state marker rather than the brightest thing on the poster. All pairs keep
    // >= 4.5:1 contrast against their own background.
    const STATUS_CONFIG = {
      'FIXED': { label: 'Working', dot: '#34d399', bg: 'bg-[#062b20] text-[#4fb188] border-[#0f5138]' },
      'UNCONFIRMED': { label: 'Testing', dot: '#e0b43c', bg: 'bg-[#2b2208] text-[#c2a03f] border-[#55430a]' },
      'UPCOMING': { label: 'Upcoming', dot: '#60a5fa', bg: 'bg-[#101a3d] text-[#6c93c9] border-[#1f3a6b]' },
      'STALLED': { label: 'Stalled', dot: '#f87171', bg: 'bg-[#2b1111] text-[#c26a6a] border-[#5e2323]' },
      'COMPLETED': { label: 'Completed', dot: '#a78bfa', bg: 'bg-[#312e81] text-[#c4b5fd] border-[#6366f1]' },
      'PAUSED': { label: 'Paused', dot: '#8b8b97', bg: 'bg-surface text-[#91919a] border-line' },
    };

    const NAV_INACTIVE_CLASS = 'nav-item w-full flex items-center gap-2.5 px-2.5 py-1.5 rounded-md text-sm font-medium transition-colors text-zinc-400 hover:text-zinc-100 hover:bg-raised';
    const NAV_ACTIVE_CLASS = 'nav-item w-full flex items-center gap-2.5 px-2.5 py-1.5 rounded-md text-sm font-medium transition-colors bg-raised2 text-white nav-marker [&>svg]:text-accent';
    const SEG_ACTIVE_CLASS = 'font-semibold bg-accent/15 text-accent-soft';
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

    function switchTab(tab) {
      // Leaving a show page through the sidebar: ask first if the form has edits.
      if (currentInspectedShowId !== null) {
        if (!confirmDiscardShowChanges()) return;
        showInitialState = null;
        clearShowHash();
        leaveShowPage(tab);
        return;
      }
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
      const colors = {
        success: 'bg-[#142618] border-[#1f4a28] text-emerald-300',
        error: 'bg-[#2b1616] border-[#4d2424] text-rose-300',
        info: 'bg-surface border-line text-zinc-200',
      }[type] || 'bg-surface border-line text-white';

      toast.className = `border rounded-xl px-3.5 py-2.5 text-[13px] shadow-xl shadow-black/40 transition-all duration-200 translate-y-2 opacity-0 flex items-center justify-between gap-3 ${colors}`;
      toast.innerHTML = `<span>${escapeHtml(message)}</span><button onclick="this.parentElement.remove()" class="text-zinc-400 hover:text-white text-xs">✕</button>`;

      document.getElementById('toast-container').appendChild(toast);
      setTimeout(() => { toast.classList.remove('translate-y-2', 'opacity-0'); }, 10);
      const dismiss = () => {
        toast.classList.add('opacity-0', 'translate-y-2');
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

    async function loadShows() {
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
        if (savedColor !== userAccent.color || savedTint !== userAccent.tint || !document.getElementById('user-accent')) {
          applyUserAccent(savedColor, savedTint);
        }
        renderShows();
        renderCalendar();
        updateStatus();
        syncShowPageHeader();
        warmAccents();
      } catch (err) {
        showToast(`Failed loading data: ${err}`, 'error');
      }
    }

    let currentSortMode = localStorage.getItem('show_sort_mode') || 'airing';

    function setSortMode(mode) {
      currentSortMode = mode;
      localStorage.setItem('show_sort_mode', mode);
      updateSortButtonStyles();
      renderShows();
    }

    function updateSortButtonStyles() {
      const modes = ['airing', 'title', 'default'];
      modes.forEach(m => {
        const btn = document.getElementById(`sort-btn-${m}`);
        if (!btn) return;
        const svg = btn.querySelector('svg');
        if (m === currentSortMode) {
          btn.className = 'px-2.5 py-1 rounded-md flex items-center gap-1.5 transition-colors bg-accent/15 text-accent-soft font-medium';
          if (svg) svg.className = 'w-3.5 h-3.5 text-accent-soft';
        } else {
          btn.className = 'px-2.5 py-1 rounded-md flex items-center gap-1.5 transition-colors text-zinc-400 hover:text-zinc-200';
          if (svg) svg.className = 'w-3.5 h-3.5 text-zinc-400';
        }
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

    function renderShows() {
      updateSortButtonStyles();
      // Completed shows sit in their own section, so they are pulled out first
      // rather than relying on them to be filtered out of Releasing/Planned.
      const completedShows = sortShowsList(allShows.filter(s => isShowCompleted(s)));
      const activeShows = allShows.filter(s => !isShowCompleted(s));
      const releasingShows = sortShowsList(activeShows.filter(s => s.is_released));
      const plannedShows = sortShowsList(activeShows.filter(s => !s.is_released));

      document.getElementById('badge-total-shows').textContent = allShows.length;
      document.getElementById('header-count-releasing').textContent = `(${releasingShows.length})`;
      document.getElementById('header-count-planned').textContent = `(${plannedShows.length})`;
      document.getElementById('header-count-completed').textContent = `(${completedShows.length})`;

      const gridReleasing = document.getElementById('grid-releasing');
      const gridPlanned = document.getElementById('grid-planned');
      const gridCompleted = document.getElementById('grid-completed');

      gridReleasing.innerHTML = releasingShows.map(renderShowCard).join('') || '<div class="col-span-full py-6 text-center text-zinc-500 text-sm">No currently releasing anime.</div>';
      gridPlanned.innerHTML = plannedShows.map(renderShowCard).join('') || '<div class="col-span-full py-6 text-center text-zinc-500 text-sm">No planned upcoming anime.</div>';
      gridCompleted.innerHTML = completedShows.map(renderShowCard).join('') || '<div class="col-span-full py-6 text-center text-zinc-500 text-sm">No completed anime.</div>';

      // Releasing is always shown so the page is never blank; the trailing
      // sections are noise when they have nothing in them.
      document.getElementById('section-completed').classList.toggle('hidden', completedShows.length === 0);
      document.getElementById('section-planned').classList.toggle('hidden', plannedShows.length === 0);
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

    function renderShowCard(show) {
      const { isPaused, isCompleted, statusKey, cfg, label } = resolveShowStatus(show);

      // Overlay line: "EP n" on the left, countdown or outcome on the right.
      const downloaded = show.downloaded_episodes_count || 0;
      const total = show.total_episodes || 0;
      const { epLabel, airInfo, airClass, countdownAttr } = showAirSummary(show, statusKey);

      // The count is shown whenever something is downloaded; the bar only when the
      // season total is known, since without it there is nothing to fill against.
      const showProgress = downloaded > 0 || total > 0;
      const showBar = total > 0;
      const pct = showBar ? Math.min(100, Math.round((downloaded / total) * 100)) : 0;
      const barClass = (isPaused || isCompleted) ? 'bg-zinc-400' : 'bg-accent';
      const progressText = `${downloaded}/${total || '?'}`;
      const hasOverlay = epLabel || airInfo || showProgress;

      const name = escapeHtml(show.display_name);
      const isDimmed = isPaused || isCompleted;
      const dimClass = isDimmed ? 'opacity-80 grayscale-[35%]' : '';
      const noArt = `<div class="absolute inset-0 flex items-center justify-center bg-surface text-zinc-600 text-xs font-mono ${dimClass}">No Art</div>`;
      const art = show.cover_image
        ? `${noArt}<img src="${escapeHtml(show.cover_image)}" alt="${name}" class="absolute inset-0 w-full h-full object-cover ${dimClass}" loading="lazy" onerror="this.onerror=null;this.style.display='none'">`
        : noArt;
      // The shade lives inside the zooming layer, so the art and its shade scale as one
      // unit and no unshaded strip of poster can show behind the episode text.
      const shade = hasOverlay ? '<div class="absolute inset-x-0 bottom-0 h-2/5 bg-gradient-to-t from-black/90 via-black/55 to-transparent"></div>' : '';
      const posterImg = `<div class="absolute inset-0 overflow-hidden"><div class="poster-zoom absolute inset-0">${art}${shade}</div></div>`;

      // Every status is a dot until the card is hovered, then it grows into its label.
      const statusChip = `
        <span class="status-chip px-1.5 py-1 inline-flex items-center rounded-full text-[11px] font-medium border ${cfg.bg}" title="${label}">
          <span class="w-1.5 h-1.5 rounded-full flex-shrink-0" style="background:${cfg.dot}"></span>
          <span class="status-label">${label}</span>
        </span>`;

      const overlay = hasOverlay ? `
            <div class="absolute inset-x-0 bottom-0 z-10 pointer-events-none px-2.5 pb-2.5">
              <div class="flex items-baseline justify-between gap-2 text-[13px]">
                <span class="font-semibold tracking-wide text-white">${epLabel}</span>
                <span class="show-countdown font-medium tabular-nums ${airClass}" ${countdownAttr} title="${localDateTime(show.next_airing_at)}">${airInfo}</span>
              </div>
              ${showProgress ? `<div class="mt-0.5 text-right text-[10px] tabular-nums text-zinc-400">${progressText}</div>` : ''}
            </div>
            ${showBar ? `<div class="absolute inset-x-0 bottom-0 h-1 z-10 bg-white/15"><div class="h-full ${barClass}" style="width:${pct}%"></div></div>` : ''}` : '';

      const pauseIcon = isPaused
        ? `<svg class="w-3.5 h-3.5 ml-0.5" fill="currentColor" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>`
        : `<svg class="w-3.5 h-3.5" fill="currentColor" viewBox="0 0 24 24"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>`;

      const feedName = show.current_feed_name ? escapeHtml(show.current_feed_name) : 'No feed';
      const lockIcon = show.feed_learned
        ? `<svg class="w-3 h-3 flex-shrink-0 text-zinc-600" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2"><title>Feed locked</title><path stroke-linecap="round" stroke-linejoin="round" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z"/></svg>`
        : '';

      return `
        <div onclick="openShowPage(${show.id})" onpointerenter="prefetchShowPage(${show.id})" class="bg-surface border ${isDimmed ? 'border-line-soft' : 'border-line'} hover:-translate-y-1 hover:shadow-md hover:shadow-black/30 rounded-lg show-card flex flex-col overflow-hidden group cursor-pointer transition-[transform,box-shadow] duration-150 ease-out">

          <div data-poster-id="${show.id}" class="relative w-full aspect-[2/3] bg-canvas overflow-hidden">
            ${posterImg}
            ${overlay}

            <div class="absolute top-2 right-2 z-20">${statusChip}</div>

            <div class="card-actions absolute top-2 left-2 flex items-center gap-1.5 z-20">
              ${!isCompleted ? `
              <button onclick="event.stopPropagation(); togglePauseShow(${show.id})" class="w-7 h-7 rounded-md flex items-center justify-center bg-black/70 hover:bg-black text-zinc-100 transition-colors active:scale-90" title="${isPaused ? 'Resume monitoring' : 'Pause monitoring'}">
                ${pauseIcon}
              </button>
              ` : ''}
              <button onclick="event.stopPropagation(); deleteShow(${show.id})" class="w-7 h-7 rounded-md flex items-center justify-center bg-black/70 hover:bg-rose-600 text-zinc-100 transition-colors active:scale-90" title="Delete show from monitoring">
                <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2.2"><path stroke-linecap="round" stroke-linejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/></svg>
              </button>
            </div>
          </div>

          <div class="px-3 pt-2.5 pb-3 flex flex-col gap-1">
            <h3 class="text-sm font-medium text-zinc-200 group-hover:text-white leading-[1.4] line-clamp-2 min-h-[2.55rem] overflow-hidden pb-[1px]" title="${name}">${name}</h3>
            <div class="flex items-center gap-1.5 text-xs text-zinc-500 min-w-0" title="${feedName}">
              <svg class="w-3 h-3 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M6 5c7.18 0 13 5.82 13 13M6 11a7 7 0 017 7m-6 0a1 1 0 11-2 0 1 1 0 012 0z"/></svg>
              <span class="truncate">${feedName}</span>
              ${lockIcon}
            </div>
          </div>

        </div>
      `;
    }

    function formatTime12(date) {
      if (!date) return '';
      let h = date.getHours();
      const m = String(date.getMinutes()).padStart(2, '0');
      const isPm = h >= 12;
      h = h % 12;
      if (h === 0) h = 12;
      const period = isPm ? 'p. m.' : 'a. m.';
      return `${h}:${m} ${period}`;
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
              const timeStr = formatTime12(showInstanceDate);
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
              const timeStr = formatTime12(baseAirDate);
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
              <span class="text-sm font-semibold ${isToday ? 'text-accent' : isPastDay ? 'text-zinc-400' : 'text-zinc-100'}">${dayName}</span>
              <span class="text-xs ${isPastDay ? 'text-zinc-500' : 'text-zinc-400'} font-medium">${dateStr}</span>
            </div>
          </div>
        `;

        let itemsHtml = '';

        if (colShows.length === 0) {
          if (isToday) {
            itemsHtml = `
              <div class="py-6 flex flex-col items-center justify-center space-y-2 select-none">
                <div class="w-full relative py-1 my-1 -ml-3 flex items-center gap-1 z-20">
                  <span class="w-2 h-2 rounded-full bg-accent absolute -left-[4px]"></span>
                  <div class="h-px w-full bg-accent/50"></div>
                </div>
                <div class="text-zinc-600 text-xs">No releases</div>
              </div>
            `;
          } else {
            itemsHtml = `<div class="py-6 text-zinc-600 text-xs select-none">No releases</div>`;
          }
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
              itemsHtml += `
                <div class="relative py-1 my-1.5 -ml-3 flex items-center gap-1 z-20 select-none">
                  <span class="w-2 h-2 rounded-full bg-accent absolute -left-[4px]"></span>
                  <div class="h-px w-full bg-accent/50"></div>
                </div>
              `;
              renderedLine = true;
            }

            itemsHtml += createTimeSlotHtml(slot, isToday);
          });

          if (isToday && !renderedLine) {
            itemsHtml += `
              <div class="relative py-1 my-1.5 -ml-3 flex items-center gap-1 z-20 select-none">
                <span class="w-2 h-2 rounded-full bg-accent absolute -left-[4px]"></span>
                <div class="h-px w-full bg-accent/50"></div>
              </div>
            `;
          }
        }

        gridHtml += `
          <div class="flex flex-col min-h-[380px] ${isPastDay ? 'opacity-85' : 'opacity-100'}">
            ${headerHtml}
            <div class="relative pl-3.5 space-y-4 flex-1 flex flex-col before:absolute before:left-[3px] before:top-2 before:bottom-2 before:w-[1.5px] before:bg-raised2">
              ${itemsHtml}
            </div>
          </div>
        `;
      }

      gridContainer.innerHTML = gridHtml;

      const badgeEl = document.getElementById('badge-calendar-shows');
      if (badgeEl) {
        badgeEl.textContent = totalCalendarShows;
      }
    }

    function createTimeSlotHtml(slot, isToday) {
      const isMulti = slot.items.length > 1;

      let dotColor = 'bg-sky-400';
      const hasFixed = slot.items.some(i => (i.show.status || '').toUpperCase() === 'FIXED');
      const hasUnconf = slot.items.some(i => (i.show.status || '').toUpperCase() === 'UNCONFIRMED');
      const hasStalled = slot.items.some(i => (i.show.status || '').toUpperCase() === 'STALLED');
      const allPaused = slot.items.every(i => i.isPaused);

      if (allPaused) {
        dotColor = 'bg-zinc-600';
      } else if (hasFixed) {
        dotColor = slot.hasPassed ? 'bg-emerald-500/85' : 'bg-emerald-400';
      } else if (hasUnconf) {
        dotColor = slot.hasPassed ? 'bg-amber-500/85' : 'bg-amber-400';
      } else if (hasStalled) {
        dotColor = slot.hasPassed ? 'bg-rose-500/85' : 'bg-rose-400';
      } else {
        dotColor = slot.hasPassed ? 'bg-sky-500/80' : 'bg-sky-400';
      }

      if (!isMulti) {
        const item = slot.items[0];
        const show = item.show;
        const statusKey = (show.status || '').toUpperCase();
        const isPaused = item.isPaused;
        const hasPassed = item.hasPassed;

        let titleColor = 'text-zinc-100 group-hover:text-sky-300';
        if (isPaused) {
          titleColor = 'text-zinc-400';
        } else if (statusKey === 'FIXED') {
          titleColor = hasPassed ? 'text-emerald-400/85 group-hover:text-emerald-300' : 'text-emerald-400 group-hover:text-emerald-300';
        } else if (statusKey === 'UNCONFIRMED') {
          titleColor = hasPassed ? 'text-amber-300/85 group-hover:text-amber-200' : 'text-amber-300 group-hover:text-amber-200';
        } else if (statusKey === 'STALLED') {
          titleColor = hasPassed ? 'text-rose-400/85 group-hover:text-rose-300' : 'text-rose-400 group-hover:text-rose-300';
        } else {
          titleColor = hasPassed ? 'text-zinc-300/85' : 'text-zinc-100 group-hover:text-sky-300';
        }

        const posterImg = show.cover_image
          ? `<img src="${show.cover_image}" alt="${show.display_name}" class="w-16 aspect-[2/3] rounded-md object-cover flex-shrink-0 bg-canvas ${isPaused ? 'grayscale' : ''}" loading="lazy" onerror="this.onerror=null;this.src='https://via.placeholder.com/100x140/1a1a20/4a4a58?text=Poster'">`
          : `<div class="w-16 aspect-[2/3] bg-canvas border border-zinc-800 rounded-md flex items-center justify-center text-[10px] font-mono text-zinc-500 flex-shrink-0">No Art</div>`;

        let epText = show.next_airing_episode ? `EP${show.next_airing_episode}` : (show.last_confirmed_episode ? `EP${show.last_confirmed_episode}` : 'EP1');
        const anilistUrl = show.anilist_id ? `https://anilist.co/anime/${show.anilist_id}` : '#';

        return `
          <div class="relative group select-none">
            <div class="flex items-center justify-between gap-1.5 mb-1.5">
              <div class="flex items-center gap-1.5">
                <span class="w-3 h-3 rounded-full ${dotColor} absolute -left-[15px] z-10"></span>
                <span class="text-[13px] tabular-nums ${isPaused ? 'text-zinc-400' : (hasPassed ? 'text-zinc-400' : 'text-zinc-100')} font-medium">${slot.timeStr}</span>
              </div>
              <span class="text-xs tabular-nums font-medium ${isPaused ? 'text-zinc-500' : (hasPassed ? 'text-zinc-500' : 'text-zinc-400')}">${epText}</span>
            </div>

            <a href="${anilistUrl}" target="_blank" rel="noopener noreferrer" class="flex gap-3 items-start cursor-pointer transition-opacity ${isPaused ? 'grayscale opacity-50 hover:opacity-80' : (hasPassed ? 'opacity-80 hover:opacity-100' : 'opacity-100 hover:opacity-90')}">
              ${posterImg}

              <div class="flex-1 min-w-0 pt-0.5">
                <h4 class="text-sm font-medium leading-snug line-clamp-3 ${titleColor}" title="${show.display_name}">
                  ${show.display_name}
                </h4>
              </div>
            </a>
          </div>
        `;
      }

      const showsHtml = slot.items.map(item => {
        const show = item.show;
        const statusKey = (show.status || '').toUpperCase();
        const isPaused = item.isPaused;
        const hasPassed = item.hasPassed;

        let titleColor = 'text-zinc-100 group-hover:text-sky-300';
        if (isPaused) {
          titleColor = 'text-zinc-400';
        } else if (statusKey === 'FIXED') {
          titleColor = hasPassed ? 'text-emerald-400/85 group-hover:text-emerald-300' : 'text-emerald-400 group-hover:text-emerald-300';
        } else if (statusKey === 'UNCONFIRMED') {
          titleColor = hasPassed ? 'text-amber-300/85 group-hover:text-amber-200' : 'text-amber-300 group-hover:text-amber-200';
        } else if (statusKey === 'STALLED') {
          titleColor = hasPassed ? 'text-rose-400/85 group-hover:text-rose-300' : 'text-rose-400 group-hover:text-rose-300';
        } else {
          titleColor = hasPassed ? 'text-zinc-300/85' : 'text-zinc-100 group-hover:text-sky-300';
        }

        const posterImg = show.cover_image
          ? `<img src="${show.cover_image}" alt="${show.display_name}" class="w-16 aspect-[2/3] rounded-md object-cover flex-shrink-0 bg-canvas ${isPaused ? 'grayscale' : ''}" loading="lazy" onerror="this.onerror=null;this.src='https://via.placeholder.com/100x140/1a1a20/4a4a58?text=Poster'">`
          : `<div class="w-16 aspect-[2/3] bg-canvas border border-zinc-800 rounded-md flex items-center justify-center text-[10px] font-mono text-zinc-500 flex-shrink-0">No Art</div>`;

        let epText = show.next_airing_episode ? `EP${show.next_airing_episode}` : (show.last_confirmed_episode ? `EP${show.last_confirmed_episode}` : 'EP1');
        const anilistUrl = show.anilist_id ? `https://anilist.co/anime/${show.anilist_id}` : '#';

        return `
          <a href="${anilistUrl}" target="_blank" rel="noopener noreferrer" class="flex gap-3 items-start cursor-pointer transition-opacity ${isPaused ? 'grayscale opacity-50 hover:opacity-80' : (hasPassed ? 'opacity-80 hover:opacity-100' : 'opacity-100 hover:opacity-90')}">
            ${posterImg}

            <div class="flex-1 min-w-0 pt-0.5">
              <div class="flex items-start justify-between gap-1">
                <h4 class="text-sm font-medium leading-snug line-clamp-3 ${titleColor}" title="${show.display_name}">
                  ${show.display_name}
                </h4>
                <span class="text-xs tabular-nums font-medium ${isPaused ? 'text-zinc-500' : (hasPassed ? 'text-zinc-500' : 'text-zinc-400')} shrink-0 ml-1">${epText}</span>
              </div>
            </div>
          </a>
        `;
      }).join('');

      return `
        <div class="relative group select-none">
          <div class="flex items-center justify-between gap-1.5 mb-2">
            <div class="flex items-center gap-1.5">
              <span class="w-3 h-3 rounded-full ${dotColor} absolute -left-[15px] z-10"></span>
              <span class="text-[13px] tabular-nums ${slot.hasPassed ? 'text-zinc-400' : 'text-zinc-100'} font-medium">${slot.timeStr}</span>
            </div>
          </div>

          <div class="space-y-3">
            ${showsHtml}
          </div>
        </div>
      `;
    }

    // Opening a show swaps the scroll container for #tab-show. Edits raise the save
    // pill; nothing is saved on leave, and leaving with unsaved edits asks first.

    let showListTab = 'episodes';
    let showReturnTab = 'shows';
    let showReturnScroll = 0;
    let showPagePushed = false;

    function setShowListTab(tab) {
      showListTab = tab;
      ['episodes', 'feed'].forEach(name => {
        const panel = document.getElementById(`show-panel-${name}`);
        const button = document.getElementById(`show-tab-${name}`);
        if (panel) panel.classList.toggle('hidden', name !== tab);
        if (button) button.className = name === tab ? SEG_ACTIVE_CLASS : SEG_INACTIVE_CLASS;
      });
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

    // `feed` is null while the feed lookup is still loading.
    function buildShowLists(showId, data, episodes, feed) {
      const isDirect = data.download_mode === 'direct';
      const loadingText = 'Checking the feed…';

      const replacements = new Map();
      ((feed && feed.feed_matches) || []).forEach(m => {
        const best = replacements.get(m.episode);
        if (m.replaces && (!best || m.version > best.version)) replacements.set(m.episode, m);
      });

      const episodeRows = episodes.map(ep => {
        const done = String(ep.status).toLowerCase() === 'completed';
        const missed = String(ep.status).toLowerCase() === 'missed';
        const statusCls = done ? 'text-emerald-400' : missed ? 'text-amber-400' : 'text-zinc-500';
        const unmanaged = isDirect && done && !ep.torrent_hash;
        const restoring = unmanaged && pendingDownloads.has(pendingKey(showId, `restore:${ep.id}`));
        const swap = isDirect && done && ep.torrent_hash ? replacements.get(ep.episode_number) : null;
        const swapPending = swap && pendingDownloads.has(pendingKey(showId, swap.title));
        return `
          <li ${rowAttrs(`e:${ep.id}`, [ep.episode_number, ep.version, ep.release_title, ep.status, ep.torrent_hash ? 1 : 0, restoring ? 1 : 0, swap ? swap.title : '', swapPending ? 1 : 0].join('|'))} class="flex items-center gap-3 py-1.5 text-xs">
            <span class="w-20 shrink-0 text-accent-soft tabular-nums">Ep ${ep.episode_number}${ep.version > 1 ? ` · v${ep.version}` : ''}</span>
            <span class="flex-1 min-w-0 truncate text-zinc-500 font-mono" title="${escapeHtml(ep.release_title || '')}">${escapeHtml(ep.release_title || '')}</span>
            <span class="shrink-0 h-7 flex items-center justify-end gap-2">
              <span class="${statusCls}">${escapeHtml(ep.status)}${done && !ep.torrent_hash ? ' · unmanaged' : ''}</span>
              ${unmanaged ? restoreButton(showId, ep) : ''}
              ${swap ? downloadButton(showId, swap.title, replaceTip(ep.episode_number, ep.feed_name), SWAP_ICON) : ''}
            </span>
          </li>`;
      }).join('');

      if (isDirect) {
        const matches = feed ? (feed.feed_matches || []) : [];
        const feedRows = matches.map(m => `
          <li ${rowAttrs(`f:${m.title}`, [m.episode, m.version, m.downloadable ? 1 : 0, m.restore ? 1 : 0, m.replaces ? 1 : 0, m.episode_status, pendingDownloads.has(pendingKey(showId, m.title)) ? 1 : 0].join('|'))} class="flex items-center gap-3 py-1.5 text-xs">
            <span class="w-20 shrink-0 text-accent-soft tabular-nums">Ep ${m.episode}${m.version > 1 ? ` · v${m.version}` : ''}</span>
            <span class="flex-1 min-w-0 truncate text-zinc-400 font-mono select-all" title="${escapeHtml(m.display_title && m.display_title !== m.title ? `${m.display_title}\n${m.title}` : m.title)}">${escapeHtml(m.display_title || m.title)}</span>
            <span class="shrink-0 w-24 h-7 flex items-center justify-end">
              ${m.downloadable
                ? (m.replaces
                  ? downloadButton(showId, m.title, replaceTip(m.episode, ''), SWAP_ICON)
                  : downloadButton(showId, m.title, m.restore
                    ? 'Restore: add this release to qBittorrent, or tag the copy already there, so Kisetsu manages it.'
                    : 'Download this release now. It is tracked like any other episode.'))
                : `<span class="text-zinc-600">${escapeHtml(m.episode_status)}</span>`}
            </span>
          </li>`).join('');
        const elsewhere = feed ? (feed.other_feeds || []) : [];
        const feedName = escapeHtml(data.feed_name || 'the assigned feed');
        const emptyFeedText = elsewhere.length
          ? `Nothing in ${feedName} matches this show. Found in ${elsewhere.map(f => `${escapeHtml(f.feed_name)} (${f.count})`).join(', ')}. Change the feed to use ${elsewhere.length === 1 ? 'it' : 'one of them'}.`
          : 'Nothing in the feed matches this show right now.';
        return `
          <div class="flex-1 min-h-0 flex flex-col gap-3">
            <div class="flex items-center justify-between gap-3">
              <div class="seg">
                <button type="button" id="show-tab-episodes" onclick="setShowListTab('episodes')" class="${showListTab === 'episodes' ? SEG_ACTIVE_CLASS : SEG_INACTIVE_CLASS}">Episodes</button>
                <button type="button" id="show-tab-feed" onclick="setShowListTab('feed')" class="${showListTab === 'feed' ? SEG_ACTIVE_CLASS : SEG_INACTIVE_CLASS}">In feed</button>
              </div>
              <span class="text-xs text-zinc-500 tabular-nums">${episodes.length} tracked · ${feed ? `${matches.length} in feed` : 'checking feed…'}</span>
            </div>
            <div id="show-panel-episodes" class="flex-1 min-h-0 flex flex-col ${showListTab === 'episodes' ? '' : 'hidden'}">${listShell(episodeRows, 'No episode records yet.')}</div>
            <div id="show-panel-feed" class="flex-1 min-h-0 flex flex-col ${showListTab === 'feed' ? '' : 'hidden'}">${listShell(feedRows, feed ? emptyFeedText : loadingText)}</div>
          </div>`;
      }

      if (!(data.has_qbit_rule && data.has_learned_pattern)) return '';
      const articles = feed ? (feed.matched_articles || []) : [];
      const count = articles.length;
      const ruleRows = articles.map(a => `
        <li ${rowAttrs(`r:${a}`, pendingDownloads.has(pendingKey(showId, a)) ? 1 : 0)} class="flex items-center gap-3 py-1.5 text-xs">
          <span class="flex-1 min-w-0 truncate text-zinc-400 font-mono select-all" title="${escapeHtml(a)}">${escapeHtml(a)}</span>
          ${downloadButton(showId, a, 'Download this release now with the same save path, category and ratio as the rule')}
        </li>`).join('');
      return `
        <div class="flex-1 min-h-0 flex flex-col gap-3">
          <div class="flex items-baseline justify-between gap-3">
            <span class="section-title">In feed</span>
            <span class="text-xs text-zinc-500">${!feed ? '' : count === 1 ? '1 match' : `${count} matches`}</span>
          </div>
          ${listShell(ruleRows, feed ? 'No cached RSS articles currently match this rule pattern.' : loadingText)}
        </div>`;
    }

    function showListsMarkup(showId, data, episodes, feed) {
      const inner = buildShowLists(showId, data, episodes, feed)
        || '<div class="flex-1 flex items-center justify-center text-center text-sm text-zinc-500 px-6 py-10">Episodes and feed matches appear here once the show has a rule and a matched release.</div>';
      return `
        <h3 class="section-title mb-2 px-1 flex items-center gap-2"><span class="w-1.5 h-1.5 rounded-full bg-accent"></span>Episodes</h3>
        <div class="card p-4 flex-1 min-h-0 flex flex-col">${inner}</div>`;
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
        result = { matched_articles: [], feed_matches: [] };
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
      document.getElementById('show-col-feed').innerHTML = block;
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

    // "EP n" plus a countdown or outcome, shared by the cards and the page header.
    function showAirSummary(show, statusKey) {
      const total = show.total_episodes || 0;
      let epLabel = '';
      let airInfo = '';
      let airClass = 'text-zinc-100';
      let countdownAttr = '';
      if (statusKey === 'COMPLETED') {
        epLabel = total ? `${total} EP` : '';
        airInfo = 'Completed';
        airClass = 'text-zinc-300';
      } else if (show.next_airing_episode && show.next_airing_at) {
        const cd = formatEpisodeCountdown(show.next_airing_at);
        if (cd === 'Aired' && show.last_confirmed_episode && show.last_confirmed_episode >= show.next_airing_episode) {
          epLabel = `EP ${show.last_confirmed_episode}`;
          airInfo = 'Downloaded';
          airClass = 'text-emerald-300';
        } else {
          epLabel = `EP ${show.next_airing_episode}`;
          airInfo = formatCardCountdown(cd, localDateTime(show.next_airing_at));
          if (cd === 'Aired') airClass = 'text-amber-300';
          countdownAttr = `data-air-at="${show.next_airing_at}" data-date-str="${localDateTime(show.next_airing_at)}"`;
        }
      } else if (show.last_confirmed_episode) {
        epLabel = `EP ${show.last_confirmed_episode}`;
      } else if (statusKey === 'STALLED') {
        airInfo = 'Stalled';
        airClass = 'text-rose-300';
      }
      return { epLabel, airInfo, airClass, countdownAttr };
    }

    function showBannerMarkup(show) {
      const src = show.banner_image || show.cover_image;
      if (!src) return '';
      const cls = show.banner_image ? 'object-cover' : 'object-cover scale-125 blur-2xl opacity-60';
      return `<img src="${escapeHtml(src)}" alt="" class="absolute inset-0 w-full h-full ${cls}" onerror="this.onerror=null;this.style.display='none'">`;
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
          ? `<img src="${escapeHtml(backdropSrc)}" alt="" class="w-full h-full object-cover scale-110 blur-3xl opacity-60 saturate-150" onerror="this.onerror=null;this.style.display='none'">`
          : '';
      }

      applyShowPalette(show.accent_hues);
      if (!show.accent_ready) fetchShowAccent(show.id);

      const dimClass = (isPaused || isCompleted) ? 'opacity-80 grayscale-[35%]' : '';
      poster.innerHTML = show.cover_image
        ? `<img src="${escapeHtml(show.cover_image)}" alt="${name}" class="absolute inset-0 w-full h-full object-cover ${dimClass}" onerror="this.onerror=null;this.style.display='none'">`
        : '<div class="absolute inset-0 flex items-center justify-center text-zinc-600 text-xs font-mono">No Art</div>';

      const alt = [show.title_romaji, show.title_english].find(t => t && t !== show.display_name);
      const season = show.season_name
        ? `${show.season_name.charAt(0)}${show.season_name.slice(1).toLowerCase()}${show.season_year ? ` ${show.season_year}` : ''}`
        : (show.season_year ? String(show.season_year) : '');
      const subtitle = [alt, season].filter(Boolean).map(escapeHtml).join(' · ');

      const downloaded = show.downloaded_episodes_count || 0;
      const total = show.total_episodes || 0;
      const showProgress = downloaded > 0 || total > 0;
      const pct = total > 0 ? Math.min(100, Math.round((downloaded / total) * 100)) : 0;
      const barClass = (isPaused || isCompleted) ? 'bg-zinc-400' : 'bg-accent';

      const feedName = show.current_feed_name ? escapeHtml(show.current_feed_name) : 'No feed';
      const lockIcon = show.feed_learned
        ? `<svg class="w-3 h-3 flex-shrink-0 text-zinc-500" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2"><title>Feed locked</title><path stroke-linecap="round" stroke-linejoin="round" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z"/></svg>`
        : '';

      const pauseIcon = isPaused
        ? '<svg class="w-3.5 h-3.5" fill="currentColor" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>'
        : '<svg class="w-3.5 h-3.5" fill="currentColor" viewBox="0 0 24 24"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>';

      const engineLine = block.dataset.engine ? `<p class="text-xs text-zinc-500 font-mono truncate">${escapeHtml(block.dataset.engine)}</p>` : '';

      block.innerHTML = `
        <div class="flex flex-wrap items-start gap-x-4 gap-y-3">
          <div class="min-w-0 flex-1 basis-64">
            <div class="flex items-center gap-2 min-w-0">
              <h2 class="text-xl md:text-2xl font-semibold text-zinc-50 leading-tight tracking-tight truncate" title="${name}">${name}</h2>
              <a href="https://anilist.co/anime/${show.anilist_id}" target="_blank" rel="noopener noreferrer" class="flex-shrink-0 text-accent-soft hover:text-white transition-colors" title="Open on AniList" aria-label="Open on AniList">
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
          <span class="px-2 py-1 inline-flex items-center gap-1.5 rounded-full font-medium border ${cfg.bg}">
            <span class="w-1.5 h-1.5 rounded-full flex-shrink-0" style="background:${cfg.dot}"></span>${label}
          </span>
          ${(epLabel || airInfo) ? `
          <span class="text-[13px]">
            <span class="font-semibold tracking-wide text-zinc-100">${epLabel}</span>
            <span class="show-countdown font-medium tabular-nums ml-1.5 ${airClass}" ${countdownAttr} title="${localDateTime(show.next_airing_at)}">${airInfo}</span>
          </span>` : ''}
          <span class="inline-flex items-center gap-1.5 text-zinc-500 min-w-0" title="${feedName}">
            <svg class="w-3 h-3 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M6 5c7.18 0 13 5.82 13 13M6 11a7 7 0 017 7m-6 0a1 1 0 11-2 0 1 1 0 012 0z"/></svg>
            <span class="truncate">${feedName}</span>${lockIcon}
          </span>
        </div>
        ${showProgress ? `
        <div class="flex items-center gap-3 max-w-sm mt-3">
          ${total > 0 ? `<div class="flex-1 h-1.5 rounded-full bg-white/10 overflow-hidden"><div class="h-full ${barClass}" style="width:${pct}%"></div></div>` : ''}
          <span class="text-xs tabular-nums text-zinc-400">${downloaded}/${total || '?'} downloaded</span>
        </div>` : ''}
        ${engineLine ? `<div class="mt-3">${engineLine}</div>` : ''}`;
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

    // `hues` is [primary hue, saturation, secondary hue].
    // How strongly the page surfaces take the hue (0 keeps them grey).
    const THEME_TINT = 0.6;

    // `hues` is [accent hue, accent saturation, secondary hue, tint hue, tint saturation].
    function paletteFromHues([h1, s1, h2, th, ts]) {
      const s = Math.min(0.85, Math.max(0.4, s1));
      const strong = hslToRgb(h1, s, 0.56);
      const warm = th >= 10 && th <= 60 ? 0.7 : 1;
      const t = Math.min(0.5, ts) * THEME_TINT * warm;
      const surface = (l, k = 1) => hslToRgb(th, t * k, l).join(' ');
      return {
        bg: surface(0.085),
        sunken: surface(0.065),
        chrome: surface(0.10),
        surface: surface(0.115),
        field: surface(0.07),
        raised: surface(0.155),
        raised2: surface(0.19),
        line: surface(0.21, 0.9),
        lineSoft: surface(0.16, 0.9),
        lineHover: surface(0.26, 0.9),
        lineStrong: surface(0.32, 0.9),
        ac: hslToRgb(h1, s, 0.62).join(' '),
        soft: hslToRgb(h1, s, 0.76).join(' '),
        strong: strong.join(' '),
        ink: relativeLuminance(strong) < 0.2 ? '255 255 255' : '8 10 14',
        ac2: hslToRgb(h2, s, 0.62).join(' '),
      };
    }

    // The variables live on <html>, so the sidebar and header bar inherit them too.
    function applyShowPalette(hues) {
      const section = document.documentElement;
      const keys = {
        '--ac': 'ac', '--ac-soft': 'soft', '--ac-strong': 'strong', '--ac-ink': 'ink', '--ac2': 'ac2',
        '--bg': 'bg', '--sunken': 'sunken', '--chrome': 'chrome', '--surface': 'surface', '--field': 'field', '--raised': 'raised', '--raised2': 'raised2',
        '--line': 'line', '--line-soft': 'lineSoft', '--line-hover': 'lineHover', '--line-strong': 'lineStrong',
      };
      const palette = hues ? paletteFromHues(hues) : null;
      Object.keys(keys).forEach(name => {
        if (palette) section.style.setProperty(name, palette[keys[name]]);
        else section.style.removeProperty(name);
      });
    }

    const DEFAULT_ACCENT = '#2dd4bf';
    const ACCENT_PRESETS = [
      ['Teal', '#2dd4bf'], ['Sky', '#38bdf8'], ['Indigo', '#818cf8'], ['Violet', '#a78bfa'], ['Pink', '#f472b6'],
      ['Rose', '#fb7185'], ['Orange', '#fb923c'], ['Amber', '#fbbf24'], ['Lime', '#a3e635'], ['Emerald', '#34d399'],
    ];
    const ACCENT_TINTS = { off: 0, subtle: 0.22, full: 0.5 };
    const ACCENT_CSS_KEY = 'kisetsu_accent_css';
    const STOCK_ACCENT = { ac: '45 212 191', soft: '94 234 212', strong: '20 184 166', ink: '4 32 30', ac2: '56 189 248' };
    let userAccent = { color: DEFAULT_ACCENT, tint: 'subtle' };

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

    function userAccentCss(color, tint) {
      const tintSat = ACCENT_TINTS[tint] ?? ACCENT_TINTS.subtle;
      if (color === DEFAULT_ACCENT && !tintSat) return '';
      const [h, sat, l] = hexToHsl(color);
      const pal = paletteFromHues([Math.round(h), sat, (Math.round(h) + 40) % 360, Math.round(h), tintSat]);
      let accent = STOCK_ACCENT;
      if (color !== DEFAULT_ACCENT) {
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
        '--ac': accent.ac, '--ac-soft': accent.soft, '--ac-strong': accent.strong, '--ac-ink': accent.ink, '--ac2': accent.ac2,
      };
      if (tintSat) {
        Object.assign(vars, {
          '--bg': pal.bg, '--sunken': pal.sunken, '--chrome': pal.chrome, '--surface': pal.surface, '--field': pal.field,
          '--raised': pal.raised, '--raised2': pal.raised2, '--line': pal.line, '--line-soft': pal.lineSoft,
          '--line-hover': pal.lineHover, '--line-strong': pal.lineStrong,
        });
      }
      return ':root{' + Object.keys(vars).map(k => k + ':' + vars[k]).join(';') + '}';
    }

    function applyUserAccent(color, tint) {
      userAccent = { color, tint };
      const css = userAccentCss(color, tint);
      let el = document.getElementById('user-accent');
      if (!el) {
        el = document.createElement('style');
        el.id = 'user-accent';
        document.head.appendChild(el);
      }
      el.textContent = css;
      try {
        if (css) localStorage.setItem(ACCENT_CSS_KEY, css);
        else localStorage.removeItem(ACCENT_CSS_KEY);
      } catch (err) {
        // The cache only avoids a flash on load.
      }
      renderAppearancePicker();
    }

    function renderAppearancePicker() {
      const box = document.getElementById('accent-swatches');
      if (!box) return;
      const isPreset = ACCENT_PRESETS.some(p => p[1] === userAccent.color);
      const ring = (active) => active ? 'ring-2 ring-offset-2 ring-offset-surface ring-white/80' : 'hover:scale-110';
      box.innerHTML = ACCENT_PRESETS.map(([name, hex]) => `
        <button type="button" title="${name}" aria-label="${name}" onclick="setUserAccent('${hex}')" style="background:${hex}" class="w-6 h-6 rounded-full transition-transform ${ring(hex === userAccent.color)}"></button>`).join('') + `
        <label title="Custom colour" class="relative w-6 h-6 rounded-full cursor-pointer overflow-hidden transition-transform ${ring(!isPreset)}" style="background:${isPreset ? 'conic-gradient(#fb7185, #fbbf24, #a3e635, #2dd4bf, #818cf8, #fb7185)' : userAccent.color}">
          <input type="color" value="${userAccent.color}" onchange="setUserAccent(this.value)" class="absolute inset-0 w-full h-full opacity-0 cursor-pointer">
        </label>`;
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
    let renderHold = null;

    function runPageTransition(swap, fromEl, toEl, kind) {
      const NAME = 'show-poster';
      const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      if (reduce) {
        swap();
        return Promise.resolve();
      }
      if (!document.startViewTransition) {
        swap();
        const entering = document.getElementById(kind === 'open' ? 'tab-show' : 'main-scroll-container');
        entering.classList.remove('page-enter');
        void entering.offsetWidth;
        entering.classList.add('page-enter');
        setTimeout(() => entering.classList.remove('page-enter'), 400);
        return Promise.resolve();
      }
      const root = document.documentElement;
      if (fromEl && fromEl.offsetParent === null) fromEl = null;
      if (fromEl) fromEl.style.viewTransitionName = NAME;
      root.classList.add(`vt-${kind}`);
      let target = null;
      const transition = document.startViewTransition(() => {
        if (fromEl) fromEl.style.viewTransitionName = '';
        swap();
        target = fromEl && toEl ? toEl() : null;
        if (target && target.offsetParent !== null) target.style.viewTransitionName = NAME;
        else target = null;
      });
      const done = transition.finished.catch(() => {}).then(() => {
        root.classList.remove(`vt-${kind}`);
        if (fromEl) fromEl.style.viewTransitionName = '';
        if (target) target.style.viewTransitionName = '';
        if (renderHold === done) renderHold = null;
      });
      // loadShows redraws every card; doing that mid-transition would delete the
      // card being morphed into and cancel the animation, so it waits.
      renderHold = done;
      return done;
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
        block.dataset.engine = '';
        renderShowHeader(known);
        loadShowPageBody(showId, true);
      };
      if (opts.instant) swap();
      else runPageTransition(swap, card, () => document.getElementById('show-poster'), 'open');
    }

    async function loadShowPageBody(showId, withSkeleton) {
      if (withSkeleton) showBodySkeleton();
      try {
        // The rule panel and the episode ledger are independent, so fetch both.
        const pending = takeShowFetch(showId);
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
        document.getElementById('show-col-feed').innerHTML = '';
        setShowListsHtml(`<div class="card p-4 flex-1 flex items-center justify-center text-rose-400 text-sm text-center">Failed loading show details: ${escapeHtml(err.message || err)}</div>`);
      }
    }

    function renderShowBody(showId, data, episodes) {
      const isCompleted = (data.status === 'completed');
      const isPaused = (data.status === 'paused');
      const isRuleActive = data.enabled === true;
      const isDirect = data.download_mode === 'direct';

      const engine = isDirect ? 'Direct download engine' : (data.rule_name || (isCompleted ? 'Completed Series' : 'No Rule Configured'));
      const titleBlock = document.getElementById('show-title-block');
      if (titleBlock.dataset.engine !== engine) {
        titleBlock.dataset.engine = engine;
        titleBlock.dataset.sig = '';
        renderShowHeader(allShows.find(s => s.id === showId));
      }

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

      const feedOptions = `<option value="0">[ Auto-discover ]</option>` +
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
        feedHint = isUpcoming ? 'Auto: first feed to post wins.' : 'Auto: testing feeds by priority.';
      }
      const feedUrlTitle = (data.feed_url && !isAutoManaged) ? escapeHtml(data.feed_url) : '';

      const noRulePlaceholder = isCompleted
        ? '<p class="field-hint !mt-0 sm:col-span-2">Completed. The rule is disabled.</p>'
        : isDirect
          ? '<p class="field-hint !mt-0 sm:col-span-2">No release found yet. The feed cache is checked now and on every check.</p>'
          : '<p class="field-hint !mt-0 sm:col-span-2">The rule arms when the first episode airs.</p>';

      const aliasesSection = data.has_rule ? `
        <div>
          <label for="show-aliases" class="field-label">Custom aliases</label>
          <input type="text" id="show-aliases" value="${escapeHtml((data.custom_aliases || []).join(', '))}" placeholder="Comma separated" class="field font-mono">
        </div>
      ` : '';

      // Rules mode: the two patterns are the qBittorrent rule itself. Direct mode
      // matches by the learned name and the aliases, so it only shows what it learned.
      const rulePatternFields = `
        <div class="sm:col-span-2">
          <label for="show-must-contain" class="field-label">Must contain (regex)</label>
          <input type="text" id="show-must-contain" value="${escapeHtml(data.must_contain || '')}" placeholder=".*" class="field font-mono">
        </div>
        <div class="sm:col-span-2">
          <label for="show-must-not-contain" class="field-label">Must not contain</label>
          <input type="text" id="show-must-not-contain" value="${escapeHtml(data.must_not_contain || '')}" placeholder="(720p|480p|...)" class="field font-mono">
        </div>`;
      const matchedAsField = `
        <div class="sm:col-span-2">
          <span class="field-label">Matched as</span>
          ${data.matched_as
            ? `<input type="text" readonly value="${escapeHtml(data.matched_as)}" onfocus="this.select()" class="field font-mono" title="${escapeHtml(data.matched_as)}">`
            : `<p class="field-hint !mt-0">${data.feed_name ? `Nothing in ${escapeHtml(data.feed_name)} matches this show yet.` : 'Matched by name and aliases until a release appears.'}</p>`}
        </div>`;
      const mustContainSections = !data.has_rule ? '' : isDirect ? matchedAsField : rulePatternFields;

      const regexSections = data.has_rule ? `${aliasesSection}${mustContainSections}` : noRulePlaceholder;

      document.getElementById('show-side-form').innerHTML = `
        <h3 class="section-title mb-2 px-1 flex items-center gap-2"><span class="w-1.5 h-1.5 rounded-full bg-accent"></span>Feed &amp; matching<span class="ml-auto text-xs font-normal text-zinc-500">${ruleStatusText}</span></h3>
        <div class="card p-4 flex flex-col gap-4">
         <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div>
            <label for="show-feed-id" class="field-label">${feedLabel}</label>
            <select id="show-feed-id" class="field" title="${feedUrlTitle}">
              ${feedOptions}
            </select>
            ${feedHint ? `<p class="field-hint">${feedHint}</p>` : ''}
          </div>
          ${regexSections}
         </div>
         <button type="button" onclick="showTriggerRediscover()" class="self-start text-sm text-zinc-400 hover:text-zinc-100 underline underline-offset-4 decoration-accent/50 hover:decoration-accent transition-colors" title="Clear manual rule customizations and let the supervisor auto-match against feeds">
          Reset to Auto-Detect
         </button>
        </div>`;

      document.getElementById('show-col-feed').innerHTML = `
        <h3 class="section-title mb-2 px-1 flex items-center gap-2"><span class="w-1.5 h-1.5 rounded-full bg-accent2"></span>Download</h3>
        <div class="card p-4 space-y-4">
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
        switchTab(tab);
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
      if (feedChanged && inspectedShow && inspectedShow.feed_learned && now.current_feed_id !== inspectedShow.learned_feed_id) {
        const lockedName = inspectedShow.learned_feed_name || 'the feed that delivered';
        const target = allFeeds.find(f => f.id === now.current_feed_id);
        const targetName = target ? `'${target.qbit_feed_name}'` : 'auto-detect';
        const ok = confirm(
          `'${inspectedShow.display_name}' is locked to '${lockedName}' because a release was recorded from it.\n\n` +
          `Move it to ${targetName} anyway? The next release downloaded will lock the show to that feed.`
        );
        if (!ok) return;
        releaseLearnedFeed = true;
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
          `'${show.display_name}' already downloaded a release from '${feedName}'.\n\n` +
          `That feed is locked and is what the app will keep using. Resetting abandons it and ` +
          `re-discovers from scratch, which may move the show to a different feed.\n\n` +
          `Continue anyway?`
        );
        if (!ok) return;
        force = true;
      }
      await rediscoverShow(showId, force);
      if (currentInspectedShowId === showId) loadShowPageBody(showId, false);
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

    async function deleteShow(id) {
      const target = allShows.find(s => s.id === id);
      const name = target ? target.display_name : `#${id}`;
      if (!confirm(`Delete '${name}' from monitoring and remove its qBittorrent rule?`)) return;
      await once(`delete-${id}`, async () => {
        try {
          const data = await apiFetch(`/api/shows/${id}`, { method: 'DELETE' });
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
      document.querySelectorAll('#feeds-table-body tr').forEach(r => {
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

      const reorderPayload = {
        feeds: allFeeds.map((f, idx) => ({ id: f.id, priority: idx + 1 }))
      };

      try {
        const data = await apiFetch('/api/feeds/reorder', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(reorderPayload)
        });
        showToast(data.message || 'Feed priorities updated.', 'success');
      } catch (err) {
        showToast(`Failed updating priority: ${err}`, 'error');
        loadFeeds();
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

    function renderFeedsTable() {
      const tbody = document.getElementById('feeds-table-body');
      tbody.innerHTML = allFeeds.map((f, idx) => `
        <tr draggable="true"
            ondragstart="handleDragStart(event, ${idx})"
            ondragover="handleDragOver(event)"
            ondragleave="handleDragLeave(event)"
            ondragend="handleDragEnd(event)"
            ondrop="handleDrop(event, ${idx})"
            class="hover:bg-raised/60 cursor-grab active:cursor-grabbing transition-colors group select-none">
          <td class="py-4 px-3 text-center text-zinc-500 group-hover:text-zinc-300">
            <svg class="w-4 h-4 mx-auto opacity-60 group-hover:opacity-100 transition-opacity" fill="currentColor" viewBox="0 0 24 24"><path d="M9 5a2 2 0 11-4 0 2 2 0 014 0zm0 7a2 2 0 11-4 0 2 2 0 014 0zm0 7a2 2 0 11-4 0 2 2 0 014 0zm10-14a2 2 0 11-4 0 2 2 0 014 0zm0 7a2 2 0 11-4 0 2 2 0 014 0zm0 7a2 2 0 11-4 0 2 2 0 014 0z"/></svg>
          </td>
          <td class="py-4 px-3 text-accent text-sm font-medium tabular-nums">
            <div class="flex items-center gap-1.5">
              <span>#${f.priority}</span>
              <span class="flex flex-col opacity-60 hover:opacity-100">
                <button onclick="event.stopPropagation(); moveFeed(${idx}, ${idx - 1})" class="leading-none text-[10px] px-1 hover:text-white disabled:opacity-20" title="Move up" ${idx === 0 ? 'disabled' : ''}>▲</button>
                <button onclick="event.stopPropagation(); moveFeed(${idx}, ${idx + 1})" class="leading-none text-[10px] px-1 hover:text-white disabled:opacity-20" title="Move down" ${idx === allFeeds.length - 1 ? 'disabled' : ''}>▼</button>
              </span>
            </div>
          </td>
          <td class="py-4 px-4">
            <div class="font-medium text-zinc-100 text-sm">${escapeHtml(f.qbit_feed_name)}</div>
            <div class="text-xs text-zinc-500 font-mono truncate max-w-2xl mt-0.5">${escapeHtml(f.qbit_feed_url)}</div>
          </td>
        </tr>
      `).join('') || '<tr><td colspan="3" class="text-center py-10 text-zinc-500 text-sm">No feeds registered.</td></tr>';
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

    async function setTitleLanguage(lang) {
      const previous = currentSettings && currentSettings.title_language;
      updateTitleLanguageUi(lang);
      try {
        await apiFetch('/api/settings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title_language: lang })
        });
        if (currentSettings) currentSettings.title_language = lang;
        await loadShows();
      } catch (err) {
        if (previous) updateTitleLanguageUi(previous);
        showToast(`Failed to update title language: ${err.message || err}`, 'error');
      }
    }

    async function loadSettings() {
      try {
        const s = await apiFetch('/api/settings');
        currentSettings = s;
        document.getElementById('set-qbit-host').value = s.qbit_host || '';
        document.getElementById('set-qbit-user').value = s.qbit_username || '';
        document.getElementById('set-qbit-pass').value = '';
        document.getElementById('set-base-dir').value = s.base_dir || '';
        document.getElementById('set-category').value = s.default_category || '';
        document.getElementById('set-ratio').value = s.default_seed_ratio ?? 1.0;
        document.getElementById('set-stall-window').value = s.stall_wait_hours ?? 24;
        document.getElementById('set-anilist-user').value = s.anilist_username || '';
        document.getElementById('set-interval').value = s.refresh_interval_minutes ?? 360;
        document.getElementById('set-early-air-tolerance').value = s.early_air_tolerance_hours ?? 6;
        updateTitleLanguageUi(s.title_language || 'english');
        updateDownloadModeUi(s.download_mode || 'rules');
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
        early_air_tolerance_hours: parseInt(document.getElementById('set-early-air-tolerance').value),
        title_language: document.getElementById('set-title-language').value,
        download_mode: document.getElementById('set-download-mode').value,
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

      try {
        const data = await apiFetch('/api/cycle/run', { method: 'POST', notice: false });
        showToast(data.message || 'Sync completed.', 'success');
        loadShows();
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
        spinner.classList.add('hidden');
        text.textContent = 'Sync Now';
      }
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
        if (manual) showToast('History refreshed.', 'info');
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
          <div class="text-xs font-medium text-zinc-400 mb-1.5 px-1 flex items-center gap-1.5"><span class="w-1 h-3 rounded-sm bg-accent/70"></span>${escapeHtml(g.label)}</div>
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
      const showName = h.show_name || (h.rule_name || '').replace(/^Direct:\\s*/, '');
      // For rules rows the rule and its pattern are the only extra facts; keep
      // them out of the way instead of printing them on every line.
      const ruleTip = (!added && (h.rule_name || h.matched_regex))
        ? `${h.rule_name || ''}${h.matched_regex ? `\n${h.matched_regex}` : ''}`
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
            <div class="text-[11px] text-zinc-600 mt-0.5 truncate max-w-[12rem]"><span class="text-accent">${verb}</span>${h.feed_name ? ` · ${escapeHtml(h.feed_name)}` : ''}</div>
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
        if (manual) showToast('Logs refreshed.', 'info');
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
        const pill = (text, cls) => `<span class="inline-block w-[3.25rem] text-center rounded px-1 py-px text-[10px] font-semibold ${cls}">${text}</span>`;
        let levelBadge = pill('INFO', 'bg-zinc-500/15 text-zinc-400');
        if (level === 'ERROR') levelBadge = pill('ERROR', 'bg-rose-500/15 text-rose-400');
        else if (level === 'WARNING' || level === 'WARN') levelBadge = pill('WARN', 'bg-amber-500/15 text-amber-400');
        else if (level === 'DEBUG') levelBadge = pill('DEBUG', 'bg-sky-500/15 text-sky-400');

        const timeStr = localTime(l.timestamp) || l.time_str || '';

        return `<div class="flex items-start gap-2.5 py-0.5 hover:bg-chrome px-1.5 rounded transition-colors leading-relaxed">
          <span class="text-accent/60 select-none text-[11px] font-mono shrink-0">${timeStr}</span>
          <span class="shrink-0 text-[11px] font-mono">${levelBadge}</span>
          <span class="text-zinc-200 break-all select-text font-mono text-[11px] flex-1">${escapeHtml(l.message || '')}</span>
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
      const text = cachedLogs.map(l => `[${localTime(l.timestamp) || l.time_str || l.timestamp}] [${l.level || 'INFO'}] ${l.message}`).join('\\n');
      navigator.clipboard.writeText(text).then(() => {
        showToast('Logs copied to clipboard!', 'success');
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
      if (totalSec < 60) return `< 1m (${totalSec}s)`;
      const totalMin = Math.floor(totalSec / 60);
      if (totalMin < 60) return `in ${totalMin}m`;
      const h = Math.floor(totalMin / 60);
      const m = totalMin % 60;
      return m > 0 ? `in ${h}h ${m}m` : `in ${h}h`;
    }

    // Set from /api/status while a check holds the cycle slot.
    let runningCheck = null;
    let statusRepoll = null;

    function tickCountdown() {
      const nextEl = document.getElementById('sidebar-next-check');
      if (nextEl && runningCheck) {
        const seconds = runningCheck.seconds + Math.round((Date.now() - runningCheck.seenAt) / 1000);
        nextEl.textContent = `Checking… ${seconds}s`;
        nextEl.title = `A ${runningCheck.label} is running.`;
      } else if (nextEl) {
        if (!targetNextCheckTime) {
          nextEl.textContent = 'Routine check';
        } else {
          const now = Date.now();
          const remSec = Math.max(0, Math.floor((targetNextCheckTime - now) / 1000));
          nextEl.textContent = formatNextCheckText(remSec);

          if (nextCheckReason) {
            const targetDate = new Date(targetNextCheckTime);
            nextEl.title = `${nextCheckReason}\nTarget Time: ${targetDate.toLocaleTimeString()}`;
          }
        }
      }

      document.querySelectorAll('.show-countdown[data-air-at]').forEach(el => {
        const airAt = el.getAttribute('data-air-at');
        const dateStr = el.getAttribute('data-date-str');
        if (airAt) {
          el.textContent = formatCardCountdown(formatEpisodeCountdown(airAt), dateStr);
        }
      });
    }

    async function updateStatus(force = false) {
      if (statusUpdateInFlight) {
        if (force) statusUpdateQueued = true;
        return;
      }
      if (!force && document.hidden) return;
      statusUpdateInFlight = true;
      try {
        const st = await apiFetch('/api/status');
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

        document.getElementById('stat-working').textContent = `${st.counts.works} Working`;
        const testingEl = document.getElementById('stat-testing');
        testingEl.textContent = `${st.counts.testing} Testing`;
        testingEl.classList.toggle('hidden', !st.counts.testing);
        document.getElementById('stat-upcoming').textContent = `${st.counts.upcoming} Upcoming`;
        const stalledEl = document.getElementById('stat-stalled');
        stalledEl.textContent = `${st.counts.stalled} Stalled`;
        stalledEl.classList.toggle('hidden', !st.counts.stalled);

        if (activeTab === 'history') loadHistory();
      } catch {
      } finally {
        statusUpdateInFlight = false;
        if (statusUpdateQueued) {
          statusUpdateQueued = false;
          updateStatus(true);
        }
      }
    }

    loadShows();
    loadHistory();
    updateStatus();
    if (showIdFromHash() !== null) openShowPage(showIdFromHash(), { push: false, instant: true });
    setInterval(updateStatus, 15000);
    setInterval(tickCountdown, 30000);
    toggleLogsAutoRefresh(true);

    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) updateStatus();
    });

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && currentInspectedShowId !== null) closeShowPage();
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
  </script>
</body>
</html>
"""
    return HTMLResponse(content=html, headers=headers)
