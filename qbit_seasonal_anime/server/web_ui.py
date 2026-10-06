from typing import Dict, Optional

from fastapi.responses import HTMLResponse

def get_web_ui_html(headers: Optional[Dict[str, str]] = None) -> HTMLResponse:
    html = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>qbit-seasonal-anime</title>
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
            canvas: '#131317',
            sunken: '#0f0f13',
            chrome: '#17171c',
            surface: '#1b1b21',
            raised: '#23232b',
            raised2: '#2b2b35',
            'line-soft': '#25252d',
            line: '#2f2f3a',
            'line-strong': '#4a4a5a',
            accent: { DEFAULT: '#2dd4bf', strong: '#14b8a6', soft: '#5eead4' },
          },
          borderRadius: { xl: '0.875rem', '2xl': '1.125rem' },
        },
      },
    };
  </script>
  <style>
    ::-webkit-scrollbar { width: 8px; height: 8px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: #2f2f3a; border-radius: 4px; }
    ::-webkit-scrollbar-thumb:hover { background: #454555; }

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
      width: 100%; background: #131317; border: 1px solid #2f2f3a; border-radius: 0.625rem;
      padding: 0.55rem 0.8rem; font-size: 0.875rem; color: #f4f4f5; outline: none;
      transition: border-color .15s, box-shadow .15s;
    }
    .field:focus { border-color: #2dd4bf; }
    .field::placeholder { color: #5d5d6b; }
    .btn {
      display: inline-flex; align-items: center; justify-content: center; gap: 0.4rem;
      font-size: 0.8125rem; font-weight: 500; padding: 0.5rem 0.9rem; border-radius: 0.625rem;
      background: #23232b; color: #e4e4e7; border: 1px solid #2f2f3a;
      transition: background-color .15s, border-color .15s, color .15s, transform .1s; cursor: pointer;
    }
    .btn:hover { background: #2b2b35; border-color: #3a3a48; }
    .btn:active { transform: scale(.97); }
    .btn:focus-visible, .nav-item:focus-visible { outline: 2px solid #2dd4bf; outline-offset: 2px; }
    .btn-primary { background: #14b8a6; border-color: #14b8a6; color: #04201e; font-weight: 600; }
    .btn-primary:hover { background: #2dd4bf; border-color: #2dd4bf; }
    #btn-run-cycle:hover { border-color: #2dd4bf; color: #5eead4; }
    .btn-danger { background: #2a1818; border-color: #4a2424; color: #f0868b; }
    .btn-danger:hover { background: #381d1d; border-color: #5e2b2b; }
    .btn-sm { padding: 0.35rem 0.7rem; font-size: 0.75rem; }
    .card { background: #1b1b21; border: 1px solid #2f2f3a; border-radius: 0.875rem; }
    .page-title { font-size: 1.125rem; font-weight: 600; color: #fafafa; letter-spacing: -0.01em; }
    .page-sub { font-size: 0.8125rem; color: #8b8b97; margin-top: 0.2rem; }
    .section-title { font-size: 0.8125rem; font-weight: 600; color: #d4d4d8; letter-spacing: 0.02em; }
    .seg { display: inline-flex; background: #131317; border: 1px solid #2f2f3a; border-radius: 0.625rem; padding: 2px; }
    .seg > button { padding: 0.3rem 0.8rem; font-size: 0.8125rem; border-radius: 0.5rem; color: #9a9aa6; transition: background-color .15s, color .15s; }
    .seg > button:hover { color: #e4e4e7; }

    @media (prefers-reduced-motion: reduce) {
      .group:hover { transform: none !important; }
    }

    @media (hover: none) {
      .card-actions { visibility: visible !important; }
    }
  </style>
</head>
<body class="bg-canvas text-zinc-100 flex h-screen overflow-hidden font-sans antialiased selection:bg-zinc-600 selection:text-white">

  <div id="sidebar-backdrop" onclick="toggleSidebar(false)" class="fixed inset-0 bg-black/60 z-30 hidden md:hidden"></div>

  <aside id="sidebar" class="fixed md:static inset-y-0 left-0 w-52 -translate-x-full md:translate-x-0 transition-transform duration-200 bg-chrome border-r border-line-soft flex flex-col flex-shrink-0 select-none z-40">

    <div class="px-4 pt-4 pb-2 flex items-center">
      <span class="text-sm font-semibold text-zinc-100">qbit-seasonal-anime</span>
    </div>

    <nav class="flex-1 px-2 py-1 space-y-0.5 overflow-y-auto">
      <button onclick="switchTab('shows')" id="nav-shows" class="nav-item w-full flex items-center gap-2.5 px-2.5 py-1.5 rounded-md text-sm font-medium transition-colors bg-raised2 text-white shadow-[inset_2px_0_0_0_#2dd4bf] [&>svg]:text-accent">
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

    <div class="p-3 border-t border-line-soft space-y-2">
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

  <main class="flex-1 flex flex-col min-w-0 bg-canvas overflow-hidden">

    <header class="h-14 border-b border-line-soft px-4 md:px-8 flex items-center justify-between gap-3 flex-shrink-0 bg-chrome">
      <div class="flex items-center gap-3 min-w-0">
        <button onclick="toggleSidebar(true)" class="md:hidden p-1.5 -ml-1.5 rounded-lg text-zinc-300 hover:bg-raised" aria-label="Open menu">
          <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M4 6h16M4 12h16M4 18h16"/></svg>
        </button>
        <h1 id="page-title" class="text-sm font-semibold text-zinc-100 truncate">Shows</h1>
      </div>

      <div class="flex items-center gap-3 text-xs font-medium flex-shrink-0">
        <span id="stat-working" class="text-emerald-400">0 Working</span>
        <span id="stat-upcoming" class="text-sky-400">0 Upcoming</span>
        <span id="stat-stalled" class="text-rose-400">0 Stalled</span>
      </div>
    </header>

    <div class="flex-1 overflow-y-auto px-4 py-5 md:px-8 md:py-7" id="main-scroll-container">

      <section id="tab-shows" class="space-y-10 max-w-[1900px]">

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
          <div id="grid-releasing" class="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6 2xl:grid-cols-8 gap-4">
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
          <div id="grid-completed" class="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6 2xl:grid-cols-8 gap-4">
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
          <div id="grid-planned" class="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6 2xl:grid-cols-8 gap-4">
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

      <section id="tab-settings" class="hidden max-w-3xl space-y-6">

        <form id="settings-form" onsubmit="saveSettings(event)" class="space-y-6">

          <div>
            <h3 class="section-title mb-2 px-1 flex items-center gap-2"><span class="w-1.5 h-1.5 rounded-full bg-accent"></span>qBittorrent</h3>
            <div class="card divide-y divide-line-soft">
          <div class="px-5 py-4 grid grid-cols-1 sm:grid-cols-[minmax(0,14rem)_1fr] gap-x-8 gap-y-2 items-start">
            <div>
            <label class="field-label !mb-0" for="set-qbit-host">Host / URL</label>
            <p class="field-hint !mt-0.5">Address of the qBittorrent Web UI.</p>
            </div>
            <div><input id="set-qbit-host" type="text" required class="field"></div>
          </div>
          <div class="px-5 py-4 grid grid-cols-1 sm:grid-cols-[minmax(0,14rem)_1fr] gap-x-8 gap-y-2 items-start">
            <div>
            <label class="field-label !mb-0" for="set-qbit-user">Username</label>
            </div>
            <div><input id="set-qbit-user" type="text" required class="field"></div>
          </div>
          <div class="px-5 py-4 grid grid-cols-1 sm:grid-cols-[minmax(0,14rem)_1fr] gap-x-8 gap-y-2 items-start">
            <div>
            <label class="field-label !mb-0" for="set-qbit-pass">Password</label>
            <p class="field-hint !mt-0.5">Leave blank to keep the current one.</p>
            </div>
            <div><input id="set-qbit-pass" type="password" placeholder="••••••••" class="field"></div>
          </div>
          <div class="px-5 py-4 grid grid-cols-1 sm:grid-cols-[minmax(0,14rem)_1fr] gap-x-8 gap-y-2 items-start">
            <div>
            <div class="field-label !mb-0">Connection</div>
            </div>
            <div><div class="flex items-center gap-3"><button type="button" onclick="testQbitConnection()" class="btn btn-sm">Test connection</button><span id="test-qbit-status" class="text-xs font-mono"></span></div></div>
          </div>
            </div>
          </div>

          <div>
            <h3 class="section-title mb-2 px-1 flex items-center gap-2"><span class="w-1.5 h-1.5 rounded-full bg-sky-400"></span>Downloads</h3>
            <div class="card divide-y divide-line-soft">
          <div class="px-5 py-4 grid grid-cols-1 sm:grid-cols-[minmax(0,14rem)_1fr] gap-x-8 gap-y-2 items-start">
            <div>
            <label class="field-label !mb-0" for="set-base-dir">Base directory</label>
            <p class="field-hint !mt-0.5">Path template for new shows. <code class="text-zinc-300 font-mono">{name}</code> becomes the show name. Blank uses qBittorrent's default.</p>
            </div>
            <div><input id="set-base-dir" type="text" placeholder="e.g. ~/Anime/{name}" class="field font-mono"></div>
          </div>
          <div class="px-5 py-4 grid grid-cols-1 sm:grid-cols-[minmax(0,14rem)_1fr] gap-x-8 gap-y-2 items-start">
            <div>
            <label class="field-label !mb-0" for="set-category">Category</label>
            <p class="field-hint !mt-0.5">Default qBittorrent category.</p>
            </div>
            <div><input id="set-category" type="text" placeholder="(blank)" class="field"></div>
          </div>
          <div class="px-5 py-4 grid grid-cols-1 sm:grid-cols-[minmax(0,14rem)_1fr] gap-x-8 gap-y-2 items-start">
            <div>
            <label class="field-label !mb-0" for="set-ratio">Seed ratio limit</label>
            <p class="field-hint !mt-0.5">Stop seeding at this ratio.</p>
            </div>
            <div><input id="set-ratio" type="number" step="0.1" min="0" required class="field"></div>
          </div>
          <div class="px-5 py-4 grid grid-cols-1 sm:grid-cols-[minmax(0,14rem)_1fr] gap-x-8 gap-y-2 items-start">
            <div>
            <label class="field-label !mb-0" for="set-stall-window">Stall grace (hours)</label>
            <p class="field-hint !mt-0.5">How long a download may sit idle before it counts as stalled.</p>
            </div>
            <div><input id="set-stall-window" type="number" min="1" required class="field"></div>
          </div>
            </div>
          </div>

          <div>
            <h3 class="section-title mb-2 px-1 flex items-center gap-2"><span class="w-1.5 h-1.5 rounded-full bg-amber-400"></span>AniList &amp; schedule</h3>
            <div class="card divide-y divide-line-soft">
          <div class="px-5 py-4 grid grid-cols-1 sm:grid-cols-[minmax(0,14rem)_1fr] gap-x-8 gap-y-2 items-start">
            <div>
            <label class="field-label !mb-0" for="set-anilist-user">AniList username</label>
            <p class="field-hint !mt-0.5">Whose watching list to follow.</p>
            </div>
            <div><input id="set-anilist-user" type="text" class="field"></div>
          </div>
          <div class="px-5 py-4 grid grid-cols-1 sm:grid-cols-[minmax(0,14rem)_1fr] gap-x-8 gap-y-2 items-start">
            <div>
            <label class="field-label !mb-0" for="set-interval">Check interval (minutes)</label>
            <p class="field-hint !mt-0.5">How often to sync AniList and check feeds.</p>
            </div>
            <div><input id="set-interval" type="number" min="5" required class="field"></div>
          </div>
          <div class="px-5 py-4 grid grid-cols-1 sm:grid-cols-[minmax(0,14rem)_1fr] gap-x-8 gap-y-2 items-start">
            <div>
            <label class="field-label !mb-0" for="set-backfill-window">Backfill window (days)</label>
            <p class="field-hint !mt-0.5">How far back to look for episodes that were missed.</p>
            </div>
            <div><input id="set-backfill-window" type="number" min="0" max="365" required class="field"></div>
          </div>
          <div class="px-5 py-4 grid grid-cols-1 sm:grid-cols-[minmax(0,14rem)_1fr] gap-x-8 gap-y-2 items-start">
            <div>
            <label class="field-label !mb-0" for="set-early-air-tolerance">Early air tolerance (hours)</label>
            <p class="field-hint !mt-0.5">Start looking this long before AniList's air time, since those times are often late. 0 waits for the stated time.</p>
            </div>
            <div><input id="set-early-air-tolerance" type="number" min="0" max="168" required class="field"></div>
          </div>
            </div>
          </div>

          <div>
            <h3 class="section-title mb-2 px-1 flex items-center gap-2"><span class="w-1.5 h-1.5 rounded-full bg-violet-400"></span>Behaviour</h3>
            <div class="card divide-y divide-line-soft">
          <div class="px-5 py-4 grid grid-cols-1 sm:grid-cols-[minmax(0,14rem)_1fr] gap-x-8 gap-y-2 items-start">
            <div>
            <div class="field-label !mb-0">Anime names</div>
            <p class="field-hint !mt-0.5">Which title to show for each anime.</p>
            </div>
            <div><div class="seg">
                <button type="button" onclick="setTitleLanguage('english')" id="btn-lang-en" class="font-semibold bg-teal-500/15 text-teal-300">English</button>
                <button type="button" onclick="setTitleLanguage('romaji')" id="btn-lang-ja" class="font-medium text-zinc-400 hover:text-zinc-200">Romaji</button>
              </div>
              <input type="hidden" id="set-title-language" value="english"></div>
          </div>
          <div class="px-5 py-4 grid grid-cols-1 sm:grid-cols-[minmax(0,14rem)_1fr] gap-x-8 gap-y-2 items-start">
            <div>
            <div class="field-label !mb-0">Download engine</div>
            <p class="field-hint !mt-0.5">Rules lets qBittorrent's RSS rules download. Direct adds torrents itself and handles v2 replacements.</p>
            </div>
            <div><div class="seg">
                <button type="button" onclick="setDownloadMode('rules')" id="btn-mode-rules" class="font-semibold bg-teal-500/15 text-teal-300">Rules</button>
                <button type="button" onclick="setDownloadMode('direct')" id="btn-mode-direct" class="font-medium text-zinc-400 hover:text-zinc-200">Direct</button>
              </div>
              <input type="hidden" id="set-download-mode" value="rules"></div>
          </div>
            </div>
          </div>

          <div class="flex justify-end">
            <button type="submit" class="btn btn-primary px-6 py-2">
              Save settings
            </button>
          </div>
        </form>

        <div class="card px-5 py-4 flex items-center justify-between gap-4">
          <div>
            <h3 class="section-title">Clear all shows</h3>
            <p class="field-hint !mt-1">Deletes every monitored show and the match history, and removes the app's RSS rules from qBittorrent. Downloaded torrents are not touched.</p>
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
  </main>

  <div id="rule-modal" onclick="if (event.target === this) closeRuleModal()" class="fixed inset-0 bg-black/70 z-50 hidden flex items-center justify-center p-3 sm:p-6">
    <div class="bg-surface border border-line rounded-xl max-w-2xl w-full flex flex-col max-h-[88vh] overflow-hidden">
      <div class="flex items-start justify-between gap-3 px-5 pt-4 pb-3 border-b border-line-soft flex-shrink-0">
        <div class="min-w-0">
          <h3 class="text-base font-semibold text-zinc-100 truncate" id="rule-modal-title">Show Details</h3>
          <p class="text-xs text-zinc-500 font-mono mt-0.5 truncate" id="rule-modal-rule-name"></p>
        </div>
        <button onclick="closeRuleModal()" class="text-zinc-500 hover:text-zinc-100 text-base p-1 -mr-1" aria-label="Close">✕</button>
      </div>

      <div id="rule-modal-content" class="flex-1 overflow-y-auto px-5 py-4 space-y-4 text-sm min-h-0">
      </div>

      <div class="flex items-center justify-between gap-3 px-5 py-3 border-t border-line-soft flex-shrink-0">
        <button type="button" onclick="modalTriggerRediscover()" class="text-sm text-zinc-400 hover:text-zinc-100 underline underline-offset-4 decoration-zinc-600 transition-colors" title="Clear manual rule customizations and let the supervisor auto-match against feeds">
          Reset to Auto-Detect
        </button>

        <div class="flex items-center gap-2">
          <button type="button" onclick="closeRuleModal()" class="btn">
            Cancel
          </button>
          <button type="button" id="btn-save-show-modal" onclick="saveShowModal()" class="btn btn-primary">
            Save changes
          </button>
        </div>
      </div>
    </div>
  </div>

  <div id="toast-container" class="fixed bottom-4 right-4 z-50 flex flex-col gap-2 max-w-xs"></div>

  <script>
    let allShows = [];
    let allFeeds = [];
    let currentSettings = {};
    let activeTab = 'shows';
    let currentInspectedShowId = null;
    let modalInitialState = null;

    // Muted status colours: dim text on near-card-tone backgrounds, so a tag reads as
    // a state marker rather than the brightest thing on the poster. All pairs keep
    // >= 4.5:1 contrast against their own background.
    const STATUS_CONFIG = {
      'FIXED': { label: 'Working', bg: 'bg-[#062b20] text-[#4fb188] border-[#0f5138]' },
      'UNCONFIRMED': { label: 'Testing', bg: 'bg-[#2b2208] text-[#c2a03f] border-[#55430a]' },
      'UPCOMING': { label: 'Upcoming', bg: 'bg-[#101a3d] text-[#6c93c9] border-[#1f3a6b]' },
      'STALLED': { label: 'Stalled', bg: 'bg-[#2b1111] text-[#c26a6a] border-[#5e2323]' },
      'COMPLETED': { label: 'Completed', bg: 'bg-[#312e81] text-[#c4b5fd] border-[#6366f1]' },
      'PAUSED': { label: 'Paused', bg: 'bg-surface text-[#91919a] border-line' },
    };

    const NAV_INACTIVE_CLASS = 'nav-item w-full flex items-center gap-2.5 px-2.5 py-1.5 rounded-md text-sm font-medium transition-colors text-zinc-400 hover:text-zinc-100 hover:bg-raised';
    const NAV_ACTIVE_CLASS = 'nav-item w-full flex items-center gap-2.5 px-2.5 py-1.5 rounded-md text-sm font-medium transition-colors bg-raised2 text-white shadow-[inset_2px_0_0_0_#2dd4bf] [&>svg]:text-accent';
    const SEG_ACTIVE_CLASS = 'font-semibold bg-teal-500/15 text-teal-300';
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

    function showToast(message, type = 'info') {
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
      setTimeout(() => {
        toast.classList.add('opacity-0', 'translate-y-2');
        setTimeout(() => toast.remove(), 250);
      }, 3500);
    }

    async function apiFetch(url, options = {}) {
      const res = await fetch(url, options);
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
    }

    async function loadShows() {
      try {
        const [showsData, feedsData, settingsData] = await Promise.all([
          apiFetch('/api/shows'),
          apiFetch('/api/feeds'),
          apiFetch('/api/settings')
        ]);
        allShows = showsData;
        allFeeds = feedsData;
        currentSettings = settingsData;
        renderShows();
        renderCalendar();
        updateStatus();
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
          btn.className = 'px-2.5 py-1 rounded-md flex items-center gap-1.5 transition-colors bg-teal-500/15 text-teal-300 font-medium';
          if (svg) svg.className = 'w-3.5 h-3.5 text-teal-300';
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

    function renderShowCard(show) {
      const rawStatus = (show.status || 'UNCONFIRMED').toUpperCase();
      const isPaused = rawStatus === 'PAUSED';
      const isCompleted = isShowCompleted(show);
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

      let airInfo = '-';
      let countdownAttr = '';
      if (statusKey === 'COMPLETED') {
        airInfo = 'Completed';
      } else if (show.next_airing_episode && show.next_airing_at) {
        const cd = formatEpisodeCountdown(show.next_airing_at);
        if (cd === 'Aired' && show.last_confirmed_episode && show.last_confirmed_episode >= show.next_airing_episode) {
          airInfo = `Ep ${show.last_confirmed_episode} (Downloaded)`;
        } else {
          airInfo = `Ep ${show.next_airing_episode} (${cd || show.next_airing_formatted || ''})`;
          countdownAttr = `data-air-at="${show.next_airing_at}" data-ep="${show.next_airing_episode}" data-date-str="${show.next_airing_formatted || ''}"`;
        }
      } else if (show.last_confirmed_episode) {
        airInfo = `Ep ${show.last_confirmed_episode}`;
      } else if (statusKey === 'STALLED') {
        airInfo = 'Stalled';
      }

      const feedName = show.current_feed_name || '[None]';

      const isDimmed = isPaused || isCompleted;
      const posterImg = show.cover_image 
        ? `<img src="${show.cover_image}" alt="${show.display_name}" class="w-full h-full object-cover ${isDimmed ? 'opacity-80 grayscale-[35%]' : ''}" loading="lazy" onerror="this.onerror=null;this.src='https://via.placeholder.com/260x360/1a1a20/4a4a58?text=Poster'">`
        : `<div class="w-full h-full flex items-center justify-center bg-surface text-zinc-600 text-xs font-mono ${isDimmed ? 'opacity-80 grayscale-[35%]' : ''}">No Art</div>`;

      const pauseBtnBg = 'bg-black/70 hover:bg-black text-zinc-100';

      const pauseIcon = isPaused 
        ? `<svg class="w-4 h-4 ml-0.5" fill="currentColor" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>`
        : `<svg class="w-4 h-4" fill="currentColor" viewBox="0 0 24 24"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>`;

      return `
        <div onclick="viewShowRule(${show.id})" onpointerenter="prefetchShowModal(${show.id})" class="bg-surface border ${isDimmed ? 'border-line-soft' : 'border-line'} hover:-translate-y-1 hover:shadow-md hover:shadow-black/30 rounded-xl flex flex-col overflow-hidden group cursor-pointer transition-[transform,box-shadow] duration-150 ease-out">
          
          <div class="relative w-full aspect-[2/3] bg-canvas overflow-hidden">
            ${posterImg}

            <div class="absolute top-2 right-2 z-10">
              <span class="inline-block px-2 py-0.5 rounded-full text-[11px] font-medium border ${cfg.bg}">
                ${label}
              </span>
            </div>

            <div class="card-actions absolute bottom-2 inset-x-2 flex items-center justify-between z-20 invisible group-hover:visible pointer-events-none">
              
              <div class="flex items-center gap-1.5 pointer-events-auto">
                ${!isCompleted ? `
                <button onclick="event.stopPropagation(); togglePauseShow(${show.id})" class="w-8 h-8 rounded-lg flex items-center justify-center ${pauseBtnBg} transition-colors active:scale-90" title="${isPaused ? 'Resume monitoring' : 'Pause monitoring'}">
                  ${pauseIcon}
                </button>
                ` : ''}
              </div>

              <div class="pointer-events-auto">
                <button onclick="event.stopPropagation(); deleteShow(${show.id}, '${show.display_name.replace(/'/g, "\\'")}')" class="w-8 h-8 rounded-lg flex items-center justify-center bg-black/70 hover:bg-rose-600 text-zinc-100 transition-colors active:scale-90" title="Delete show from monitoring">
                  <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2.2"><path stroke-linecap="round" stroke-linejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/></svg>
                </button>
              </div>
            </div>
          </div>

          <div class="p-3 flex flex-col justify-between bg-surface min-h-[6rem]">
            <h3 class="text-[13px] font-medium text-zinc-200 group-hover:text-white leading-[1.4] line-clamp-2 min-h-[2.55rem] overflow-hidden pb-[1px]" title="${show.display_name}">
              ${show.display_name}
            </h3>

            <div class="pt-2.5 border-t border-line-soft flex items-center justify-between gap-2.5 text-xs">
              <span class="truncate text-zinc-500 min-w-0" title="${feedName}">${feedName}</span>
              <span class="show-countdown flex-shrink-0 text-zinc-200 font-medium tabular-nums ml-auto" ${countdownAttr} title="${show.next_airing_formatted ? show.next_airing_formatted : ''}">${airInfo}</span>
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

    let modalListTab = 'episodes';

    function setModalListTab(tab) {
      modalListTab = tab;
      ['episodes', 'feed'].forEach(name => {
        const panel = document.getElementById(`modal-panel-${name}`);
        const button = document.getElementById(`modal-tab-${name}`);
        if (panel) panel.classList.toggle('hidden', name !== tab);
        if (button) button.className = name === tab ? SEG_ACTIVE_CLASS : SEG_INACTIVE_CLASS;
      });
    }

    const DOWNLOAD_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" class="w-3.5 h-3.5"><path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/></svg>';

    function downloadButton(showId, title, tip) {
      return `<button type="button" data-title="${escapeHtml(title)}" onclick="quickDownloadMatch(${showId}, this)"
        class="shrink-0 w-7 h-7 rounded-md flex items-center justify-center bg-raised hover:bg-teal-500/15 border border-line hover:border-teal-500/50 text-zinc-400 hover:text-teal-300 transition-colors active:scale-95"
        title="${escapeHtml(tip)}">${DOWNLOAD_ICON}</button>`;
    }

    function listShell(rows, emptyText) {
      return `<ul class="bg-canvas border border-line-soft rounded-lg px-3 max-h-48 overflow-y-auto divide-y divide-line-soft">${rows || `<li class="text-xs text-zinc-600 py-3 text-center">${emptyText}</li>`}</ul>`;
    }

    // `feed` is null while the feed lookup is still loading.
    function buildModalLists(showId, data, episodes, feed) {
      const isDirect = data.download_mode === 'direct';
      const loadingText = 'Checking the feed…';

      const episodeRows = episodes.map(ep => {
        const done = String(ep.status).toLowerCase() === 'completed';
        const missed = String(ep.status).toLowerCase() === 'missed';
        const statusCls = done ? 'text-emerald-400' : missed ? 'text-amber-400' : 'text-zinc-500';
        return `
          <li class="flex items-center gap-3 py-1.5 text-xs">
            <span class="w-20 shrink-0 text-zinc-200 tabular-nums">Ep ${ep.episode_number}${ep.version > 1 ? ` · v${ep.version}` : ''}</span>
            <span class="flex-1 min-w-0 truncate text-zinc-500 font-mono" title="${escapeHtml(ep.release_title || '')}">${escapeHtml(ep.release_title || '')}</span>
            <span class="shrink-0 ${statusCls}">${escapeHtml(ep.status)}</span>
          </li>`;
      }).join('');

      if (isDirect) {
        const matches = feed ? (feed.feed_matches || []) : [];
        const feedRows = matches.map(m => `
          <li class="flex items-center gap-3 py-1.5 text-xs">
            <span class="w-20 shrink-0 text-zinc-200 tabular-nums">Ep ${m.episode}${m.version > 1 ? ` · v${m.version}` : ''}</span>
            <span class="flex-1 min-w-0 truncate text-zinc-400 font-mono select-all" title="${escapeHtml(m.title)}">${escapeHtml(m.title)}</span>
            ${m.downloadable
              ? downloadButton(showId, m.title, 'Download this release now. It is tracked like any other episode.')
              : `<span class="shrink-0 text-zinc-600">${escapeHtml(m.episode_status)}</span>`}
          </li>`).join('');
        return `
          <div class="space-y-2">
            <div class="flex items-center justify-between gap-3">
              <div class="seg">
                <button type="button" id="modal-tab-episodes" onclick="setModalListTab('episodes')" class="${modalListTab === 'episodes' ? SEG_ACTIVE_CLASS : SEG_INACTIVE_CLASS}">Episodes</button>
                <button type="button" id="modal-tab-feed" onclick="setModalListTab('feed')" class="${modalListTab === 'feed' ? SEG_ACTIVE_CLASS : SEG_INACTIVE_CLASS}">In feed</button>
              </div>
              <span class="text-xs text-zinc-500 tabular-nums">${episodes.length} tracked · ${feed ? `${matches.length} in feed` : 'checking feed…'}</span>
            </div>
            <div id="modal-panel-episodes" class="${modalListTab === 'episodes' ? '' : 'hidden'}">${listShell(episodeRows, 'No episode records yet.')}</div>
            <div id="modal-panel-feed" class="${modalListTab === 'feed' ? '' : 'hidden'}">${listShell(feedRows, feed ? 'Nothing in the feed matches this show right now.' : loadingText)}</div>
          </div>`;
      }

      if (!(data.has_qbit_rule && data.has_learned_pattern)) return '';
      const articles = feed ? (feed.matched_articles || []) : [];
      const count = articles.length;
      const ruleRows = articles.map(a => `
        <li class="flex items-center gap-3 py-1.5 text-xs">
          <span class="flex-1 min-w-0 truncate text-zinc-400 font-mono select-all" title="${escapeHtml(a)}">${escapeHtml(a)}</span>
          ${downloadButton(showId, a, 'Download this release now with the same save path, category and ratio as the rule')}
        </li>`).join('');
      return `
        <div class="space-y-2">
          <div class="flex items-baseline justify-between gap-3">
            <span class="field-label !mb-0">In feed</span>
            <span class="text-xs text-zinc-500">${!feed ? '' : count === 1 ? '1 match' : `${count} matches`}</span>
          </div>
          ${listShell(ruleRows, feed ? 'No cached RSS articles currently match this rule pattern.' : loadingText)}
        </div>`;
    }

    // What the open dialog was drawn from, so the lists can be redrawn when the
    // slower feed lookup lands or after a download.
    let modalContext = null;
    let modalFeedState = null;

    function rerenderModalLists() {
      const target = document.getElementById('modal-lists');
      if (!target || !modalContext || currentInspectedShowId !== modalContext.showId) return;
      target.innerHTML = buildModalLists(modalContext.showId, modalContext.data, modalContext.episodes, modalFeedState);
    }

    async function loadModalFeedMatches(showId) {
      let result;
      try {
        result = await apiFetch(`/api/shows/${showId}/feed-matches`);
      } catch (err) {
        result = { matched_articles: [], feed_matches: [] };
      }
      if (currentInspectedShowId !== showId) return;
      modalFeedState = result;
      rerenderModalLists();
    }

    async function refreshModalLists(showId) {
      if (!modalContext || currentInspectedShowId !== showId) return;
      try {
        const episodes = await apiFetch(`/api/shows/${showId}/episodes`);
        if (currentInspectedShowId !== showId) return;
        modalContext.episodes = Array.isArray(episodes) ? episodes : [];
        rerenderModalLists();
      } catch (err) {
        // The lists are informational; the toast from the download already reported the outcome.
      }
      loadModalFeedMatches(showId);
    }

    // Started when the pointer enters a card, so the data is usually there by the
    // time the click lands. An entry is used once, then dropped, so reopening a
    // show after an edit never shows stale values.
    const modalPrefetch = new Map();
    const PREFETCH_MAX_AGE_MS = 10000;

    function startModalFetch(showId) {
      const entry = {
        at: Date.now(),
        rule: apiFetch(`/api/shows/${showId}/rule`),
        episodes: apiFetch(`/api/shows/${showId}/episodes`).catch(() => []),
      };
      entry.rule.catch(() => {});
      return entry;
    }

    function prefetchShowModal(showId) {
      const existing = modalPrefetch.get(showId);
      if (existing && Date.now() - existing.at < PREFETCH_MAX_AGE_MS) return;
      modalPrefetch.set(showId, startModalFetch(showId));
    }

    function takeModalFetch(showId) {
      const entry = modalPrefetch.get(showId);
      modalPrefetch.delete(showId);
      if (entry && Date.now() - entry.at < PREFETCH_MAX_AGE_MS) return entry;
      return startModalFetch(showId);
    }

    function modalSkeleton() {
      const block = (h) => `<div class="${h} rounded-lg bg-raised/60"></div>`;
      return `<div class="space-y-4">${block('h-16')}${block('h-16')}${block('h-16')}${block('h-24')}</div>`;
    }

    async function viewShowRule(showId) {
      currentInspectedShowId = showId;
      modalContext = null;
      modalFeedState = null;
      const modal = document.getElementById('rule-modal');
      const titleEl = document.getElementById('rule-modal-title');
      const ruleNameEl = document.getElementById('rule-modal-rule-name');
      const contentEl = document.getElementById('rule-modal-content');

      // Draw the frame now from what the page already knows; the form fills in
      // as soon as the (cheap) detail request returns.
      const known = allShows.find(sh => sh.id === showId);
      titleEl.textContent = known ? known.display_name : 'Show Details';
      ruleNameEl.textContent = '';
      contentEl.innerHTML = modalSkeleton();
      modal.classList.remove('hidden');

      try {
        // The rule panel and the episode ledger are independent, so fetch both.
        const pending = takeModalFetch(showId);
        const [data, episodes] = await Promise.all([pending.rule, pending.episodes]);
        if (currentInspectedShowId !== showId) return;

        const isCompleted = (data.status === 'completed');
        const isPaused = (data.status === 'paused');
        const isRuleActive = data.enabled === true;
        const isDirect = data.download_mode === 'direct';

        titleEl.textContent = data.display_name;
        ruleNameEl.textContent = isDirect ? 'Direct download engine' : (data.rule_name || (isCompleted ? 'Completed Series' : 'No Rule Configured'));

        // Plain muted text, not a badge: the header already names the engine.
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
          feedHint = `Locked to ${escapeHtml(data.learned_feed_name || 'the feed that delivered')}: a release was already downloaded from it, so no other feed is checked.`;
        } else if (data.feed_pinned) {
          feedHint = 'Pinned by you. Auto-detect will not move this show to another feed.';
        } else if (data.candidate_feed_id) {
          feedHint = `Watching ${escapeHtml(data.candidate_feed_name || 'another feed')}: a release appeared there but not on the preferred feed. It moves automatically if that stays true for 5 minutes.`;
        } else if (!isCompleted && data.has_rule && (isUpcoming || isTesting)) {
          feedHint = isUpcoming
            ? 'Auto-discover is on. No release seen yet, so every feed is checked and the first to post the episode wins.'
            : 'Auto-discover is on and still testing: feeds are re-checked in priority order.';
        }
        const feedUrlLine = (data.feed_url && !isAutoManaged) ? `<span class="font-mono truncate block">${escapeHtml(data.feed_url)}</span>` : '';

        const noRulePlaceholder = isCompleted
          ? '<p class="field-hint !mt-0">Completed series. All episodes aired and were confirmed downloaded, and the RSS rule is disabled.</p>'
          : '<p class="field-hint !mt-0">Upcoming show. The supervisor arms the rule when the first episode drops.</p>';

        const aliasesSection = data.has_rule ? `
          <div>
            <label for="modal-aliases" class="field-label">Custom aliases</label>
            <input type="text" id="modal-aliases" value="${escapeHtml((data.custom_aliases || []).join(', '))}" class="field font-mono">
            <p class="field-hint">Comma separated. Extra names the release group uses. AniList titles and synonyms always match too.</p>
          </div>
        ` : '';

        // Must Contain and Must Not Contain are qBittorrent RSS-rule fields. The
        // direct engine owns no rule and matches from the episode ledger, so
        // showing them there would display a regex that nothing ever reads.
        const mustContainSections = data.has_qbit_rule ? `
          <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div>
              <label for="modal-must-contain" class="field-label">Must contain (regex)</label>
              <input type="text" id="modal-must-contain" value="${escapeHtml(data.must_contain || '')}" placeholder=".*" class="field font-mono">
            </div>
            <div>
              <label for="modal-must-not-contain" class="field-label">Must not contain</label>
              <input type="text" id="modal-must-not-contain" value="${escapeHtml(data.must_not_contain || '')}" placeholder="(720p|480p|...)" class="field font-mono">
            </div>
          </div>
        ` : '';

        const regexSections = data.has_rule ? `${aliasesSection}${mustContainSections}` : noRulePlaceholder;

        contentEl.innerHTML = `
          <div>
            <div class="flex items-baseline justify-between gap-3">
              <label for="modal-feed-id" class="field-label">${feedLabel}</label>
              <span class="text-xs text-zinc-500">${ruleStatusText}</span>
            </div>
            <select id="modal-feed-id" class="field">
              ${feedOptions}
            </select>
            ${(feedHint || feedUrlLine) ? `<div class="field-hint">${feedHint ? `<p>${feedHint}</p>` : ''}${feedUrlLine}</div>` : ''}
          </div>

          ${regexSections}

          <div>
            <label for="modal-save-path" class="field-label">Save path</label>
            <input type="text" id="modal-save-path" value="${escapeHtml(data.save_path || data.save_folder || '')}" placeholder="~/Anime/${escapeHtml(data.display_name)}" class="field font-mono" title="${escapeHtml(data.save_path || '')}">
          </div>

          <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div>
              <label for="modal-category" class="field-label">Category</label>
              <input type="text" id="modal-category" value="${escapeHtml(data.category || '')}" placeholder="${escapeHtml(currentSettings.default_category || 'anime')}" class="field font-mono">
            </div>
            <div>
              <label for="modal-ratio-limit" class="field-label">Seed ratio limit</label>
              <input type="number" step="0.1" min="0" id="modal-ratio-limit" value="${data.ratio_limit !== undefined && data.ratio_limit !== null ? data.ratio_limit : ''}" placeholder="${currentSettings.default_seed_ratio || 1.0}" class="field font-mono">
            </div>
          </div>

          <div id="modal-lists">${buildModalLists(showId, data, episodes, null)}</div>
        `;
        modalInitialState = {
          // Tracks what the selector shows, not the internal feed id: while a show
          // is undecided it renders as Auto-discover, and saving that untouched must
          // not wipe the default feed the supervisor is probing.
          current_feed_id: selectedFeedId,
          save_folder: (data.save_path || data.save_folder || '').trim(),
          category: (data.category || '').trim(),
          ratio_limit: data.ratio_limit !== undefined && data.ratio_limit !== null ? parseFloat(data.ratio_limit) : undefined,
          must_contain: (data.must_contain || '').trim(),
          must_not_contain: (data.must_not_contain || '').trim(),
          aliases: (data.custom_aliases || []).join(', '),
        };
        modalContext = { showId, data, episodes: Array.isArray(episodes) ? episodes : [] };
        loadModalFeedMatches(showId);
      } catch (err) {
        contentEl.innerHTML = `<div class="text-rose-400 py-4 text-center">Failed loading show details: ${err}</div>`;
      }
    }

    function closeRuleModal() {
      document.getElementById('rule-modal').classList.add('hidden');
      currentInspectedShowId = null;
      modalInitialState = null;
    }

    async function saveShowModal() {
      if (!currentInspectedShowId) return;

      const feedSelect = document.getElementById('modal-feed-id');
      const savePathInput = document.getElementById('modal-save-path');
      const categoryInput = document.getElementById('modal-category');
      const ratioInput = document.getElementById('modal-ratio-limit');
      const mustContainInput = document.getElementById('modal-must-contain');
      const mustNotContainInput = document.getElementById('modal-must-not-contain');
      const aliasesInput = document.getElementById('modal-aliases');

      const currentFeedId = feedSelect ? parseInt(feedSelect.value) : 0;
      const currentSaveFolder = savePathInput ? savePathInput.value.trim() : '';
      const currentCategory = categoryInput ? categoryInput.value.trim() : '';
      const currentRatio = ratioInput && ratioInput.value !== '' ? parseFloat(ratioInput.value) : undefined;
      const currentMustContain = mustContainInput ? mustContainInput.value.trim() : '';
      const currentMustNotContain = mustNotContainInput ? mustNotContainInput.value.trim() : '';
      const currentAliases = aliasesInput ? aliasesInput.value.trim() : '';

      const hasChanged = !modalInitialState || (
        currentFeedId !== modalInitialState.current_feed_id ||
        currentSaveFolder !== modalInitialState.save_folder ||
        currentCategory !== modalInitialState.category ||
        currentRatio !== modalInitialState.ratio_limit ||
        currentMustContain !== modalInitialState.must_contain ||
        currentMustNotContain !== modalInitialState.must_not_contain ||
        currentAliases !== modalInitialState.aliases
      );

      if (!hasChanged) {
        closeRuleModal();
        return;
      }

      // Picking another feed for a show locked to the one that delivered is an
      // explicit override; confirm it, then tell the server to release the lock.
      const feedChanged = !modalInitialState || currentFeedId !== modalInitialState.current_feed_id;
      const inspectedShow = allShows.find(s => s.id === currentInspectedShowId);
      let releaseLearnedFeed = false;
      if (feedChanged && inspectedShow && inspectedShow.feed_learned && currentFeedId !== inspectedShow.learned_feed_id) {
        const lockedName = inspectedShow.learned_feed_name || 'the feed that delivered';
        const target = allFeeds.find(f => f.id === currentFeedId);
        const targetName = target ? `'${target.qbit_feed_name}'` : 'auto-detect';
        const ok = confirm(
          `'${inspectedShow.display_name}' is locked to '${lockedName}' because a release was recorded from it.\n\n` +
          `Move it to ${targetName} anyway? The next release downloaded will lock the show to that feed.`
        );
        if (!ok) return;
        releaseLearnedFeed = true;
      }

      const btn = document.getElementById('btn-save-show-modal');
      btn.disabled = true;
      btn.textContent = 'Saving...';

      const isRegexChanged = modalInitialState && currentMustContain !== modalInitialState.must_contain;
      const isMustNotChanged = modalInitialState && currentMustNotContain !== modalInitialState.must_not_contain;
      const isAliasesChanged = modalInitialState && currentAliases !== modalInitialState.aliases;
      const parsedAliases = currentAliases
        ? currentAliases.split(',').map(a => a.trim()).filter(a => a.length > 0)
        : [];

      const payload = {
        // Only sent when the feed was actually changed: an explicit pick pins the
        // feed, while saving other fields must leave auto-detect in charge.
        current_feed_id: feedChanged ? currentFeedId : undefined,
        release_learned_feed: releaseLearnedFeed || undefined,
        // The field shows the resolved path, so it is only sent when edited;
        // otherwise every save would pin the show to today's absolute path.
        // An emptied field resets the show to the default folder.
        save_folder: !modalInitialState || currentSaveFolder !== modalInitialState.save_folder ? currentSaveFolder : undefined,
        category: currentCategory || undefined,
        ratio_limit: currentRatio,
        must_contain: isRegexChanged ? currentMustContain : undefined,
        must_not_contain: isMustNotChanged ? currentMustNotContain : undefined,
        aliases: isAliasesChanged ? parsedAliases : undefined,
      };

      try {
        const data = await apiFetch(`/api/shows/${currentInspectedShowId}/edit`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        showToast(data.message || 'Show updated.', 'success');
        closeRuleModal();
        loadShows();
      } catch (err) {
        showToast(`Save failed: ${err.message || err}`, 'error');
      } finally {
        btn.disabled = false;
        btn.textContent = 'Save Changes';
      }
    }

    async function modalTriggerRediscover() {
      if (!currentInspectedShowId) return;
      const showId = currentInspectedShowId;
      const show = allShows.find(s => s.id === showId);
      if (show && show.feed_learned) {
        const feedName = show.learned_feed_name || show.current_feed_name || 'the feed that delivered';
        const ok = confirm(
          `'${show.display_name}' already downloaded a release from '${feedName}'.\n\n` +
          `That feed is locked and is what the app will keep using. Resetting abandons it and ` +
          `re-discovers from scratch, which may move the show to a different feed.\n\n` +
          `Continue anyway?`
        );
        if (!ok) return;
        closeRuleModal();
        await rediscoverShow(showId, true);
        return;
      }
      closeRuleModal();
      await rediscoverShow(showId, false);
    }

    async function quickDownloadMatch(showId, btn) {
      const title = btn.dataset.title;
      if (!title) return;

      btn.disabled = true;
      btn.classList.add('opacity-50', 'pointer-events-none');

      try {
        const data = await apiFetch(`/api/shows/${showId}/quick-download`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title })
        });
        showToast(data.message || 'Download started.', 'success');
        refreshModalLists(showId);
      } catch (err) {
        showToast(err.message || 'Download failed.', err.status === 409 ? 'info' : 'error');
      } finally {
        btn.disabled = false;
        btn.classList.remove('opacity-50', 'pointer-events-none');
      }
    }

    async function togglePauseShow(id) {
      try {
        const data = await apiFetch(`/api/shows/${id}/pause`, { method: 'POST' });
        showToast(data.message, 'success');
        loadShows();
      } catch (err) {
        showToast(`Action failed: ${err.message || err}`, 'error');
      }
    }

    async function rediscoverShow(id, force = false) {
      try {
        const data = await apiFetch(`/api/shows/${id}/rediscover${force ? '?force=true' : ''}`, { method: 'POST' });
        showToast(data.message, 'success');
        loadShows();
      } catch (err) {
        showToast(`Action failed: ${err.message || err}`, 'error');
      }
    }

    async function deleteShow(id, name) {
      if (!confirm(`Delete '${name}' from monitoring and remove its qBittorrent rule?`)) return;
      try {
        const data = await apiFetch(`/api/shows/${id}`, { method: 'DELETE' });
        showToast(data.message, 'success');
        loadShows();
      } catch (err) {
        showToast(`Delete failed: ${err.message || err}`, 'error');
      }
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
      if (mode === 'direct' && !confirm(
        'Switch to Direct downloads? Managed RSS rules will be disabled before Direct mode is enabled. Episode replacement operations will be managed by the application.'
      )) {
        // The user backed out, so the control has to show the real mode again.
        updateDownloadModeUi(currentSettings.download_mode || 'rules');
        return;
      }
      try {
        const res = await fetch('/api/settings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ download_mode: mode })
        });
        const data = await res.json();
        if (!res.ok) {
          updateDownloadModeUi(currentSettings.download_mode || 'rules');
          throw new Error(data.detail || data.message || 'Mode switch failed');
        }
        showToast(data.message || `Download engine switched to ${mode}.`, 'success');
        await loadSettings();
        await loadShows();
        updateStatus(true);
      } catch (err) {
        showToast(`Mode switch failed: ${err}`, 'error');
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
        const res = await fetch('/api/settings');
        const s = await res.json();
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
        document.getElementById('set-backfill-window').value = s.backfill_window_days ?? 14;
        document.getElementById('set-early-air-tolerance').value = s.early_air_tolerance_hours ?? 6;
        updateTitleLanguageUi(s.title_language || 'english');
        updateDownloadModeUi(s.download_mode || 'rules');
      } catch (err) {
        showToast(`Failed loading settings: ${err}`, 'error');
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
        backfill_window_days: parseInt(document.getElementById('set-backfill-window').value),
        early_air_tolerance_hours: parseInt(document.getElementById('set-early-air-tolerance').value),
        title_language: document.getElementById('set-title-language').value,
        download_mode: document.getElementById('set-download-mode').value,
      };
      const pwd = document.getElementById('set-qbit-pass').value;
      if (pwd) payload.qbit_password = pwd;

      try {
        const res = await fetch('/api/settings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Failed to save settings.');
        showToast(data.message || 'Settings saved.', 'success');
        await loadSettings();
        await loadShows();
      } catch (err) {
        showToast(`Failed saving settings: ${err}`, 'error');
      }
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
        const res = await fetch('/api/settings/test-qbit', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (res.ok) {
          statusEl.textContent = `Connected (qBit ${data.app_version})`;
          statusEl.className = 'text-xs font-mono text-emerald-400 font-semibold';
        } else {
          statusEl.textContent = `${data.detail}`;
          statusEl.className = 'text-xs font-mono text-rose-400';
        }
      } catch (err) {
        statusEl.textContent = `Failed: ${err}`;
        statusEl.className = 'text-xs font-mono text-rose-400';
      }
    }

    async function clearAllShows() {
      if (!confirm('Delete ALL monitored shows and remove all qBittorrent RSS rules?')) return;
      try {
        const data = await apiFetch('/api/settings/clear-all', { method: 'POST' });
        showToast(data.message || 'All shows cleared.', 'success');
        loadShows();
      } catch (err) {
        showToast(`Clear failed: ${err}`, 'error');
      }
    }

    async function runCycleNow() {
      const btn = document.getElementById('btn-run-cycle');
      const spinner = document.getElementById('spinner-run-cycle');
      const text = document.getElementById('text-run-cycle');

      btn.disabled = true;
      spinner.classList.remove('hidden');
      text.textContent = 'Syncing...';

      try {
        const data = await apiFetch('/api/cycle/run', { method: 'POST' });
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
        const res = await fetch('/api/history?limit=100');
        cachedHistory = await res.json();
        renderHistory();
        if (manual) showToast('History refreshed.', 'info');
      } catch (err) {
        if (manual) showToast(`Failed to load history: ${err}`, 'error');
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
            <div class="text-[11px] text-zinc-600 mt-0.5 truncate max-w-[12rem]"><span class="${added ? 'text-emerald-400' : 'text-sky-400'}">${verb}</span>${h.feed_name ? ` · ${escapeHtml(h.feed_name)}` : ''}</div>
          </div>
        </div>
      `;
    }

    async function clearHistory() {
      if (!confirm('Clear all match history?')) return;
      try {
        await apiFetch('/api/history', { method: 'DELETE' });
        showToast('Match history cleared.', 'success');
        loadHistory();
      } catch (err) {
        showToast(`Failed to clear history: ${err}`, 'error');
      }
    }

    let logsAutoRefreshInterval = null;
    let cachedLogs = [];


    async function loadLogs(manual = false) {
      try {
        const res = await fetch('/api/logs?limit=250');
        cachedLogs = await res.json();
        renderLogs();
        if (manual) showToast('Logs refreshed.', 'info');
      } catch (err) {
        if (manual) showToast(`Failed to load logs: ${err}`, 'error');
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

        const timeStr = l.time_str || (l.timestamp ? new Date(l.timestamp).toLocaleTimeString() : '');

        return `<div class="flex items-start gap-2.5 py-0.5 hover:bg-chrome px-1.5 rounded transition-colors leading-relaxed">
          <span class="text-teal-500/60 select-none text-[11px] font-mono shrink-0">${timeStr}</span>
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
      const text = cachedLogs.map(l => `[${l.time_str || l.timestamp}] [${l.level || 'INFO'}] ${l.message}`).join('\\n');
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

    function tickCountdown() {
      const nextEl = document.getElementById('sidebar-next-check');
      if (nextEl) {
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
        const ep = el.getAttribute('data-ep');
        const dateStr = el.getAttribute('data-date-str');
        if (airAt && ep) {
          const cd = formatEpisodeCountdown(airAt);
          el.textContent = `Ep ${ep} (${cd || dateStr || ''})`;
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

        tickCountdown();

        document.getElementById('stat-working').textContent = `${st.counts.works} Working`;
        document.getElementById('stat-upcoming').textContent = `${st.counts.upcoming} Upcoming`;
        document.getElementById('stat-stalled').textContent = `${st.counts.stalled} Stalled`;

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
    setInterval(updateStatus, 15000);
    setInterval(tickCountdown, 30000);
    toggleLogsAutoRefresh(true);

    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) updateStatus();
    });

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        const modal = document.getElementById('rule-modal');
        if (modal && !modal.classList.contains('hidden')) {
          saveShowModal();
        }
      }
    });
  </script>
</body>
</html>
"""
    return HTMLResponse(content=html, headers=headers)
