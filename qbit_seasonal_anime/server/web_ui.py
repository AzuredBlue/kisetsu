from typing import Dict, Optional

from fastapi.responses import HTMLResponse

def get_web_ui_html(headers: Optional[Dict[str, str]] = None) -> HTMLResponse:
    html = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>qbit-seasonal-anime</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    ::-webkit-scrollbar { width: 5px; height: 5px; }
    ::-webkit-scrollbar-track { background: #121215; }
    ::-webkit-scrollbar-thumb { background: #2a2a32; border-radius: 2px; }
    ::-webkit-scrollbar-thumb:hover { background: #383844; }

    .line-clamp-2 {
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }
  </style>
</head>
<body class="bg-[#121215] text-zinc-100 flex h-screen overflow-hidden font-sans antialiased selection:bg-zinc-700 selection:text-white">

  <aside class="w-60 bg-[#18181c] border-r border-[#26262e] flex flex-col flex-shrink-0 select-none z-20">
    
    <nav class="flex-1 px-3 py-5 space-y-1.5 overflow-y-auto">
      <button onclick="switchTab('shows')" id="nav-shows" class="nav-item w-full flex items-center gap-3 px-3.5 py-2.5 rounded-lg text-sm font-medium transition-colors bg-[#262630] text-white">
        <svg class="w-5 h-5 text-zinc-300" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M7 4v16M17 4v16M3 8h4m10 0h4M3 12h18M3 16h4m10 0h4M4 20h16a1 1 0 001-1V5a1 1 0 00-1-1H4a1 1 0 00-1 1v14a1 1 0 001 1z"/></svg>
        <span>Shows</span>
        <span id="badge-total-shows" class="ml-auto text-xs text-zinc-400 font-mono font-semibold">0</span>
      </button>

      <button onclick="switchTab('calendar')" id="nav-calendar" class="nav-item w-full flex items-center gap-3 px-3.5 py-2.5 rounded-lg text-sm font-medium transition-colors text-zinc-400 hover:text-zinc-200 hover:bg-[#202026]">
        <svg class="w-5 h-5 text-zinc-400" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z"/></svg>
        <span>Calendar</span>
        <span id="badge-calendar-shows" class="ml-auto text-xs text-zinc-400 font-mono font-semibold">0</span>
      </button>

      <button onclick="switchTab('history')" id="nav-history" class="nav-item w-full flex items-center gap-3 px-3.5 py-2.5 rounded-lg text-sm font-medium transition-colors text-zinc-400 hover:text-zinc-200 hover:bg-[#202026]">
        <svg class="w-5 h-5 text-zinc-400" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
        <span>History</span>
        <span id="badge-total-history" class="ml-auto text-xs text-zinc-400 font-mono font-semibold">0</span>
      </button>

      <button onclick="switchTab('feeds')" id="nav-feeds" class="nav-item w-full flex items-center gap-3 px-3.5 py-2.5 rounded-lg text-sm font-medium transition-colors text-zinc-400 hover:text-zinc-200 hover:bg-[#202026]">

        <svg class="w-5 h-5 text-zinc-400" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M6 5c7.18 0 13 5.82 13 13M6 11a7 7 0 017 7m-6 0a1 1 0 11-2 0 1 1 0 012 0z"/></svg>
        <span>RSS Feeds</span>
      </button>

      <button onclick="switchTab('settings')" id="nav-settings" class="nav-item w-full flex items-center gap-3 px-3.5 py-2.5 rounded-lg text-sm font-medium transition-colors text-zinc-400 hover:text-zinc-200 hover:bg-[#202026]">
        <svg class="w-5 h-5 text-zinc-400" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"/><path stroke-linecap="round" stroke-linejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/></svg>
        <span>Settings</span>
      </button>

      <button onclick="switchTab('logs')" id="nav-logs" class="nav-item w-full flex items-center gap-3 px-3.5 py-2.5 rounded-lg text-sm font-medium transition-colors text-zinc-400 hover:text-zinc-200 hover:bg-[#202026]">
        <svg class="w-5 h-5 text-zinc-400" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z"/></svg>
        <span>Logs</span>
      </button>
    </nav>

    <div class="p-3.5 border-t border-[#26262e] space-y-2.5 text-xs">
      <div class="space-y-0.5">
        <div class="text-[10px] text-zinc-500 uppercase tracking-wider font-semibold">Next Check</div>
        <div id="sidebar-next-check" class="text-zinc-300 font-mono text-xs leading-tight" title="">Calculating...</div>
      </div>

      <button onclick="runCycleNow()" id="btn-run-cycle" class="w-full bg-[#24242c] hover:bg-[#2e2e38] text-zinc-200 text-xs sm:text-sm py-2 px-3 rounded-lg border border-[#33333d] transition-colors flex items-center justify-center gap-2 font-medium shadow-sm active:scale-95">
        <svg id="spinner-run-cycle" class="w-4 h-4 hidden animate-spin text-zinc-400" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z"></path></svg>
        <span id="text-run-cycle">Check for New Episodes</span>
      </button>
    </div>
  </aside>

  <main class="flex-1 flex flex-col min-w-0 bg-[#121215] overflow-hidden">
    
    <header class="h-11 border-b border-[#222228] px-6 flex items-center justify-between flex-shrink-0 bg-[#16161a]">
      <div class="flex items-center gap-3 text-xs font-mono">
        <span id="stat-working" class="text-emerald-400 font-medium">0 Working</span>
        <span class="text-zinc-600">/</span>
        <span id="stat-upcoming" class="text-sky-400 font-medium">0 Upcoming</span>
        <span class="text-zinc-600">/</span>
        <span id="stat-stalled" class="text-rose-400 font-medium">0 Stalled</span>
      </div>

    </header>

    <div class="flex-1 overflow-y-auto p-6" id="main-scroll-container">

      <section id="tab-shows" class="space-y-8 max-w-[1900px]">
        
        <div id="section-releasing" class="space-y-3">
          <div class="flex items-center justify-between border-b border-[#222228] pb-1.5">
            <div class="flex items-center gap-2">
              <h2 class="text-xs font-bold uppercase tracking-wider text-zinc-300">Releasing</h2>
              <span id="header-count-releasing" class="text-xs text-zinc-500 font-mono">(0)</span>
            </div>

            <div class="flex items-center gap-1 bg-[#121215] p-0.5 rounded border border-[#26262e] text-[11px] font-mono">
              <button onclick="setSortMode('airing')" id="sort-btn-airing" class="px-2 py-0.5 rounded flex items-center gap-1.5 transition-all text-zinc-400 hover:text-zinc-200" title="Sort by Next Episode Airing (Soonest first)">
                <svg class="w-3.5 h-3.5 text-zinc-400" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
                <span>Airing</span>
              </button>
              <button onclick="setSortMode('title')" id="sort-btn-title" class="px-2 py-0.5 rounded flex items-center gap-1.5 transition-all text-zinc-400 hover:text-zinc-200" title="Sort Alphabetically (A to Z)">
                <svg class="w-3.5 h-3.5 text-zinc-400" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M3 4h13M3 8h9m-9 4h6m4 0l4-4m0 0l4 4m-4-4v12"/></svg>
                <span>A-Z</span>
              </button>
              <button onclick="setSortMode('default')" id="sort-btn-default" class="px-2 py-0.5 rounded flex items-center gap-1.5 transition-all text-zinc-400 hover:text-zinc-200" title="Default Order (Date added)">
                <svg class="w-3.5 h-3.5 text-zinc-400" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M4 6h16M4 10h16M4 14h16M4 18h16"/></svg>
                <span>Default</span>
              </button>
            </div>
          </div>
          <div id="grid-releasing" class="grid grid-cols-3 sm:grid-cols-4 md:grid-cols-5 lg:grid-cols-6 xl:grid-cols-7 2xl:grid-cols-8 gap-3">
          </div>
        </div>

        <div id="section-completed" class="space-y-3 pt-2">
          <div class="flex items-center gap-2 border-b border-[#222228] pb-1.5">
            <h2 class="text-xs font-bold uppercase tracking-wider text-zinc-300">Completed</h2>
            <span id="header-count-completed" class="text-xs text-zinc-500 font-mono">(0)</span>
          </div>
          <div id="grid-completed" class="grid grid-cols-3 sm:grid-cols-4 md:grid-cols-5 lg:grid-cols-6 xl:grid-cols-7 2xl:grid-cols-8 gap-3">
          </div>
        </div>

        <div id="section-planned" class="space-y-3 pt-2">
          <div class="flex items-center gap-2 border-b border-[#222228] pb-1.5">
            <h2 class="text-xs font-bold uppercase tracking-wider text-zinc-300">Planned</h2>
            <span id="header-count-planned" class="text-xs text-zinc-500 font-mono">(0)</span>
          </div>
          <div id="grid-planned" class="grid grid-cols-3 sm:grid-cols-4 md:grid-cols-5 lg:grid-cols-6 xl:grid-cols-7 2xl:grid-cols-8 gap-3">
          </div>
        </div>

      </section>

      <section id="tab-calendar" class="space-y-4 max-w-[1950px] hidden">
        
        <div class="flex items-center justify-between border-b border-[#222228] pb-3">
          <div>
            <h2 class="text-sm font-bold uppercase tracking-wider text-zinc-200">Weekly Calendar</h2>
            <span id="calendar-week-range" class="text-xs font-mono text-zinc-400 mt-0.5 block"></span>
          </div>
        </div>

        <div id="calendar-weekly-grid" class="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 xl:grid-cols-7 gap-6 items-start select-none">
        </div>

      </section>

      <section id="tab-history" class="space-y-4 max-w-5xl hidden">
        <div class="flex items-center justify-between border-b border-[#222228] pb-3">
          <div>
            <h2 class="text-sm font-bold uppercase tracking-wider text-zinc-200">Match History</h2>
            <p class="text-xs text-zinc-400 mt-0.5">Recorded releases matched by rules in qBittorrent</p>
          </div>

          <div class="flex items-center gap-2">
            <button onclick="loadHistory(true)" class="bg-[#1e1e24] hover:bg-[#282832] text-zinc-300 text-xs py-1.5 px-3 rounded-lg border border-[#30303c] transition-colors font-medium flex items-center gap-1.5">
              <svg class="w-3.5 h-3.5 text-zinc-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg>
              <span>Refresh</span>
            </button>
            <button onclick="clearHistory()" class="bg-[#1e1e24] hover:bg-[#282832] text-zinc-400 hover:text-rose-400 text-xs py-1.5 px-3 rounded-lg border border-[#30303c] transition-colors font-medium">
              Clear
            </button>
          </div>
        </div>

        <div id="history-container" class="space-y-2.5"></div>
      </section>

      <section id="tab-feeds" class="hidden max-w-5xl space-y-5">
        <div class="flex items-center justify-between border-b border-[#222228] pb-3">
          <div>
            <h2 class="text-sm font-bold uppercase tracking-wider text-zinc-200">RSS Feeds & Priority</h2>
            <p class="text-xs text-zinc-400 mt-0.5">Manage prioritized torrent indexer feeds queried during supervision cycles.</p>
          </div>
          <button onclick="syncFeeds()" class="bg-[#222228] hover:bg-[#2c2c34] text-zinc-200 text-xs py-2 px-4 rounded-lg border border-[#33333d] transition-colors font-medium">
            Sync Feeds from qBittorrent
          </button>
        </div>

        <div class="bg-[#18181c] border border-[#26262e] rounded-xl overflow-hidden shadow-sm">
          <table class="w-full text-left text-sm">
            <thead class="bg-[#121215] text-zinc-400 border-b border-[#26262e] uppercase text-[11px] font-semibold tracking-wider">
              <tr>
                <th class="py-3.5 px-3 w-10 text-center"></th>
                <th class="py-3.5 px-3 w-20">Priority</th>
                <th class="py-3.5 px-4">Feed Name & RSS URL</th>
              </tr>
            </thead>
            <tbody id="feeds-table-body" class="divide-y divide-[#222228]">
            </tbody>
          </table>
        </div>
      </section>

      <section id="tab-settings" class="hidden max-w-4xl space-y-6">
        <div class="border-b border-[#222228] pb-3">
          <h2 class="text-sm font-bold uppercase tracking-wider text-zinc-200">Settings</h2>
          <p class="text-xs text-zinc-400 mt-0.5">Configure daemon connectivity, paths, defaults, and scheduling.</p>
        </div>

        <form id="settings-form" onsubmit="saveSettings(event)" class="space-y-5">
          
          <div class="bg-[#18181c] border border-[#26262e] rounded-xl p-5 space-y-4 shadow-sm">
            <h3 class="text-xs font-bold text-zinc-200 uppercase tracking-wide">qBittorrent Connection</h3>

            <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <div>
                <label class="block text-xs text-zinc-300 font-medium mb-1.5">Host / URL</label>
                <input type="text" id="set-qbit-host" required class="w-full bg-[#121215] border border-[#30303a] rounded-lg px-3.5 py-2 text-sm text-zinc-100 focus:outline-none focus:border-zinc-500">
              </div>
              <div>
                <label class="block text-xs text-zinc-300 font-medium mb-1.5">Username</label>
                <input type="text" id="set-qbit-user" required class="w-full bg-[#121215] border border-[#30303a] rounded-lg px-3.5 py-2 text-sm text-zinc-100 focus:outline-none focus:border-zinc-500">
              </div>
              <div class="sm:col-span-2">
                <label class="block text-xs text-zinc-300 font-medium mb-1.5">Password <span class="text-zinc-500 font-normal">(leave blank to keep unchanged)</span></label>
                <input type="password" id="set-qbit-pass" placeholder="••••••••" class="w-full bg-[#121215] border border-[#30303a] rounded-lg px-3.5 py-2 text-sm text-zinc-100 focus:outline-none focus:border-zinc-500">
              </div>
            </div>

            <div class="pt-1 flex items-center gap-3">
              <button type="button" onclick="testQbitConnection()" class="bg-[#222228] hover:bg-[#2c2c34] text-zinc-200 text-xs py-1.5 px-3.5 rounded-lg border border-[#33333d] transition-colors font-medium">
                Test Connection
              </button>
              <span id="test-qbit-status" class="text-xs font-mono"></span>
            </div>
          </div>

          <div class="bg-[#18181c] border border-[#26262e] rounded-xl p-5 space-y-4 shadow-sm">
            <h3 class="text-xs font-bold text-zinc-200 uppercase tracking-wide">Paths & Rule Defaults</h3>

            <div class="grid grid-cols-1 sm:grid-cols-3 gap-4">
              <div class="sm:col-span-3">
                <label class="block text-xs text-zinc-200 font-medium mb-1.5">Base Download Directory <span class="text-zinc-400 font-normal">(uses <code class="text-sky-300">{name}</code> placeholder)</span></label>
                <input type="text" id="set-base-dir" placeholder="e.g. ~/Anime/{name} or D:/Anime/{name} (leave blank for qBit default)" class="w-full bg-[#121215] border border-[#30303a] rounded-lg px-3.5 py-2 text-sm text-zinc-100 font-mono focus:outline-none focus:border-zinc-500">
                <p class="text-xs text-zinc-400 mt-1">Leave blank to use qBittorrent's default download location, or enter a custom path template with <code class="text-sky-300 font-mono">{name}</code>.</p>
              </div>
              <div>
                <label class="block text-xs text-zinc-300 font-medium mb-1.5">Default Category</label>
                <input type="text" id="set-category" placeholder="(blank)" class="w-full bg-[#121215] border border-[#30303a] rounded-lg px-3.5 py-2 text-sm text-zinc-100 focus:outline-none focus:border-zinc-500">
              </div>
              <div>
                <label class="block text-xs text-zinc-300 font-medium mb-1.5">Seed Ratio Limit</label>
                <input type="number" step="0.1" min="0" id="set-ratio" required class="w-full bg-[#121215] border border-[#30303a] rounded-lg px-3.5 py-2 text-sm text-zinc-100 focus:outline-none focus:border-zinc-500">
              </div>
              <div>
                <label class="block text-xs text-zinc-300 font-medium mb-1.5">Stall Grace (Hours)</label>
                <input type="number" min="1" id="set-stall-window" required class="w-full bg-[#121215] border border-[#30303a] rounded-lg px-3.5 py-2 text-sm text-zinc-100 focus:outline-none focus:border-zinc-500">
              </div>
            </div>
          </div>

          <div class="bg-[#18181c] border border-[#26262e] rounded-xl p-5 space-y-4 shadow-sm">
            <h3 class="text-xs font-bold text-zinc-200 uppercase tracking-wide">AniList Account & Scheduler</h3>

            <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <div>
                <label class="block text-xs text-zinc-300 font-medium mb-1.5">AniList Username</label>
                <input type="text" id="set-anilist-user" class="w-full bg-[#121215] border border-[#30303a] rounded-lg px-3.5 py-2 text-sm text-zinc-100 focus:outline-none focus:border-zinc-500">
              </div>
              <div>
                <label class="block text-xs text-zinc-300 font-medium mb-1.5">Routine Refresh (Minutes)</label>
                <input type="number" min="5" id="set-interval" required class="w-full bg-[#121215] border border-[#30303a] rounded-lg px-3.5 py-2 text-sm text-zinc-100 focus:outline-none focus:border-zinc-500">
              </div>
              <div>
                <label class="block text-xs text-zinc-300 font-medium mb-1.5">Automatic Backfill Window (Days)</label>
                <input type="number" min="0" max="365" id="set-backfill-window" required class="w-full bg-[#121215] border border-[#30303a] rounded-lg px-3.5 py-2 text-sm text-zinc-100 focus:outline-none focus:border-zinc-500">
              </div>
              <div>
                <label class="block text-xs text-zinc-300 font-medium mb-1.5">Early Air Tolerance (Hours)</label>
                <input type="number" min="0" max="168" id="set-early-air-tolerance" required class="w-full bg-[#121215] border border-[#30303a] rounded-lg px-3.5 py-2 text-sm text-zinc-100 focus:outline-none focus:border-zinc-500">
                <p class="text-xs text-zinc-500 mt-1.5">Start looking this many hours before AniList's air time. AniList times are often late, and a release is taken as soon as it appears. Use 0 to only act after the stated time.</p>
              </div>
            </div>

            <div class="pt-2 border-t border-[#26262e] space-y-2">
              <div class="flex items-center justify-between gap-3">
                <button type="button" onclick="syncAniListNow()" class="bg-[#222228] hover:bg-[#2c2c34] text-zinc-200 text-xs py-1.5 px-3.5 rounded-lg border border-[#33333d] transition-colors font-medium">
                  Sync AniList Shows
                </button>

                <div class="flex items-center gap-2.5 flex-shrink-0">
                  <span class="text-xs text-zinc-300 font-medium">Anime Names</span>
                  <div class="inline-flex items-center bg-[#121215] border border-[#30303a] rounded-lg p-0.5 text-xs font-mono select-none">
                    <button type="button" onclick="setTitleLanguage('english')" id="btn-lang-en" class="px-2.5 py-1 font-semibold rounded transition-colors text-sky-400 bg-[#262632] shadow-sm">EN</button>
                    <button type="button" onclick="setTitleLanguage('romaji')" id="btn-lang-ja" class="px-2.5 py-1 font-medium rounded transition-colors text-zinc-400 hover:text-zinc-200">JA</button>
                  </div>
                  <input type="hidden" id="set-title-language" value="english">
                </div>
              </div>

              <div class="space-y-2">
                <div class="flex items-center justify-between gap-3">
                  <span class="text-xs text-zinc-300 font-medium">Download Engine</span>
                  <div class="inline-flex items-center bg-[#121215] border border-[#30303a] rounded-lg p-0.5 text-xs font-mono select-none">
                    <button type="button" onclick="setDownloadMode('rules')" id="btn-mode-rules" class="px-2.5 py-1 font-semibold rounded transition-colors text-sky-400 bg-[#262632] shadow-sm">Rules</button>
                    <button type="button" onclick="setDownloadMode('observe')" id="btn-mode-observe" class="px-2.5 py-1 font-medium rounded transition-colors text-zinc-400 hover:text-zinc-200">Observe</button>
                    <button type="button" onclick="setDownloadMode('direct')" id="btn-mode-direct" class="px-2.5 py-1 font-medium rounded transition-colors text-zinc-400 hover:text-zinc-200">Direct</button>
                  </div>
                  <input type="hidden" id="set-download-mode" value="rules">
                </div>
                <p class="text-xs text-zinc-500">Rules owns downloads. Observe logs direct decisions without adding torrents. Direct manages episodes and verified replacements.</p>
              </div>

              <div id="sync-anilist-status" class="text-xs font-mono empty:hidden"></div>
            </div>
          </div>

          <div class="flex items-center justify-between pt-2">
            <button type="submit" class="bg-blue-600 hover:bg-blue-500 text-white text-xs sm:text-sm font-semibold py-2.5 px-6 rounded-lg transition-colors shadow-md">
              Save Settings
            </button>

            <button type="button" onclick="clearAllShows()" class="bg-[#261818] hover:bg-[#361c1c] text-rose-400 text-xs py-2 px-4 rounded-lg border border-[#442222] transition-colors">
              Clear All Monitored Shows
            </button>
          </div>
        </form>
      </section>

      <section id="tab-logs" class="space-y-4 max-w-6xl hidden">
        <div class="flex items-center justify-between">
          <div>
            <h2 class="text-xl font-bold text-white tracking-tight">Supervisor & System Logs</h2>
            <p class="text-xs text-zinc-400 mt-1">Live execution trace of scheduled supervisor cycles, AniList updates, RSS discoveries, and rule state changes.</p>
          </div>

          <div class="flex items-center gap-2">
            <label class="flex items-center gap-2 text-xs text-zinc-400 bg-[#18181c] border border-[#26262e] px-3 py-1.5 rounded-lg cursor-pointer hover:text-zinc-200">
              <input type="checkbox" id="logs-auto-refresh" checked onchange="toggleLogsAutoRefresh(this.checked)" class="rounded bg-[#121215] border-[#30303a] text-blue-500 focus:ring-0">
              <span>Auto-refresh</span>
            </label>
            <button onclick="loadLogs(true)" class="bg-[#1e1e24] hover:bg-[#282832] text-zinc-300 text-xs py-1.5 px-3 rounded-lg border border-[#30303c] transition-colors font-medium flex items-center gap-1.5">
              <svg class="w-3.5 h-3.5 text-zinc-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg>
              <span>Refresh</span>
            </button>
            <button onclick="copyLogs()" class="bg-[#1e1e24] hover:bg-[#282832] text-zinc-300 text-xs py-1.5 px-3 rounded-lg border border-[#30303c] transition-colors font-medium flex items-center gap-1.5">
              <svg class="w-3.5 h-3.5 text-zinc-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z"/></svg>
              <span>Copy</span>
            </button>
          </div>
        </div>

        <div class="bg-[#101014] border border-[#24242c] rounded-xl overflow-hidden shadow-xl flex flex-col h-[calc(100vh-210px)]">
          <div class="h-9 bg-[#16161b] border-b border-[#24242c] px-4 flex items-center justify-between flex-shrink-0 text-xs font-mono text-zinc-400">
            <div class="flex items-center gap-2">
              <span class="w-2 h-2 rounded-full bg-emerald-500 inline-block animate-pulse"></span>
              <span class="text-zinc-300 font-medium">Activity Stream</span>
            </div>
            <div id="logs-count-badge" class="text-zinc-500 text-[11px]">0 entries</div>
          </div>

          <div id="logs-container" class="flex-1 p-4 font-mono text-xs overflow-y-auto space-y-1.5 bg-[#0e0e12] select-text">
            <div class="text-zinc-600 text-center py-12">Loading supervisor logs...</div>
          </div>
        </div>
      </section>

    </div>
  </main>

  <div id="rule-modal" onclick="if (event.target === this) closeRuleModal()" class="fixed inset-0 bg-black/80 backdrop-blur-sm z-50 hidden flex items-center justify-center p-3 sm:p-6">
    <div class="bg-[#18181c] border border-[#2e2e38] rounded-2xl max-w-3xl w-full p-5 sm:p-6 space-y-3.5 shadow-2xl overflow-y-auto max-h-[92vh]">
      <div class="flex items-center justify-between border-b border-[#26262e] pb-2.5">
        <div>
          <h3 class="text-base font-bold uppercase tracking-wider text-zinc-100 truncate pr-2" id="rule-modal-title">Show Details</h3>
          <p class="text-xs text-zinc-400 font-mono mt-0.5" id="rule-modal-rule-name"></p>
        </div>
        <button onclick="closeRuleModal()" class="text-zinc-400 hover:text-zinc-100 text-base font-bold p-1">✕</button>
      </div>

      <div id="rule-modal-content" class="space-y-3 text-sm">
      </div>

      <div class="flex items-center justify-between pt-3 border-t border-[#26262e]">
        <button type="button" onclick="modalTriggerRediscover()" class="text-xs sm:text-sm text-sky-400 hover:text-sky-300 py-1 font-medium transition-colors" title="Clear manual rule customizations and let the supervisor auto-match against feeds">
          Reset to Auto-Detect
        </button>

        <div class="flex items-center gap-2.5">
          <button type="button" onclick="closeRuleModal()" class="bg-[#222228] hover:bg-[#2a2a32] text-zinc-300 text-xs sm:text-sm py-2 px-4 rounded-lg border border-[#33333d] transition-colors">
            Cancel
          </button>
          <button type="button" id="btn-save-show-modal" onclick="saveShowModal()" class="bg-blue-600 hover:bg-blue-500 text-white text-xs sm:text-sm font-medium py-2 px-5 rounded-lg transition-colors shadow-md">
            Save Changes
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
      'PAUSED': { label: 'Paused', bg: 'bg-[#1b1b1f] text-[#91919a] border-[#2e2e35]' },
    };

    function switchTab(tab) {
      activeTab = tab;
      document.querySelectorAll('nav button').forEach(b => {
        b.className = 'nav-item w-full flex items-center gap-3 px-3.5 py-2.5 rounded-lg text-sm font-medium transition-colors text-zinc-400 hover:text-zinc-200 hover:bg-[#202026]';
      });
      const activeBtn = document.getElementById(`nav-${tab}`);
      if (activeBtn) {
        activeBtn.className = 'nav-item w-full flex items-center gap-3 px-3.5 py-2.5 rounded-lg text-sm font-medium transition-colors bg-[#262630] text-white';
      }

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
        info: 'bg-[#1a1a20] border-[#30303a] text-zinc-200',
      }[type] || 'bg-[#1a1a20] border-[#30303a] text-white';

      toast.className = `border rounded p-2.5 text-xs shadow-lg transition-all duration-200 translate-y-2 opacity-0 flex items-center justify-between gap-3 ${colors}`;
      toast.innerHTML = `<span>${message}</span><button onclick="this.parentElement.remove()" class="text-zinc-400 hover:text-white font-bold text-xs">✕</button>`;

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
          btn.className = 'px-2 py-0.5 rounded flex items-center gap-1.5 transition-all bg-[#26262e] text-indigo-400 font-semibold shadow-sm';
          if (svg) svg.className = 'w-3.5 h-3.5 text-indigo-400';
        } else {
          btn.className = 'px-2 py-0.5 rounded flex items-center gap-1.5 transition-all text-zinc-400 hover:text-zinc-200 hover:bg-[#18181c]';
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

      gridReleasing.innerHTML = releasingShows.map(renderShowCard).join('') || '<div class="col-span-full py-6 text-center text-zinc-500 text-xs font-mono">No currently releasing anime.</div>';
      gridPlanned.innerHTML = plannedShows.map(renderShowCard).join('') || '<div class="col-span-full py-6 text-center text-zinc-500 text-xs font-mono">No planned upcoming anime.</div>';
      gridCompleted.innerHTML = completedShows.map(renderShowCard).join('') || '<div class="col-span-full py-6 text-center text-zinc-500 text-xs font-mono">No completed anime.</div>';

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
        ? `<img src="${show.cover_image}" alt="${show.display_name}" class="w-full h-full object-cover transition-all duration-200 ${isDimmed ? 'opacity-80 grayscale-[35%]' : ''}" loading="lazy" onerror="this.onerror=null;this.src='https://via.placeholder.com/260x360/1a1a20/4a4a58?text=Poster'">`
        : `<div class="w-full h-full flex items-center justify-center bg-[#18181c] text-zinc-600 text-xs font-mono ${isDimmed ? 'opacity-80 grayscale-[35%]' : ''}">No Art</div>`;

      const pauseBtnBg = isPaused 
        ? 'bg-emerald-600 hover:bg-emerald-500 text-white' 
        : 'bg-amber-600 hover:bg-amber-500 text-white';

      const pauseIcon = isPaused 
        ? `<svg class="w-4 h-4 ml-0.5" fill="currentColor" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>`
        : `<svg class="w-4 h-4" fill="currentColor" viewBox="0 0 24 24"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>`;

      // Surface upgraded releases: a v2 episode means a replacement was verified.
      const versionLabel = show.v2_episodes_count > 0
        ? `<span class="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-bold border border-amber-800 bg-amber-950/90 text-amber-300">${show.v2_episodes_count} v2</span>`
        : '';

      return `
        <div onclick="viewShowRule(${show.id})" class="bg-[#18181c] border ${isDimmed ? 'border-zinc-800' : 'border-[#26262e]'} hover:border-[#444452] hover:-translate-y-1 hover:shadow-lg hover:shadow-black/50 rounded flex flex-col overflow-hidden group cursor-pointer transition-all duration-200 ease-out">
          
          <div class="relative w-full aspect-[2/3] bg-[#121215] overflow-hidden">
            ${posterImg}

            <div class="absolute top-2 right-2 z-10">
              <span class="inline-block px-1.5 py-0.5 rounded text-[10px] font-medium border shadow-sm ${cfg.bg}">
                ${label}
              </span>
            </div>

            <div class="absolute bottom-2 inset-x-2 flex items-center justify-between z-20 opacity-0 group-hover:opacity-100 transition-opacity duration-150 pointer-events-none">
              
              <div class="flex items-center gap-1.5 pointer-events-auto">
                ${!isCompleted ? `
                <button onclick="event.stopPropagation(); togglePauseShow(${show.id})" class="w-7 h-7 rounded flex items-center justify-center ${pauseBtnBg} shadow-md transition-transform active:scale-90" title="${isPaused ? 'Resume monitoring' : 'Pause monitoring'}">
                  ${pauseIcon}
                </button>
                ` : ''}
              </div>

              <div class="pointer-events-auto">
                <button onclick="event.stopPropagation(); deleteShow(${show.id}, '${show.display_name.replace(/'/g, "\\'")}')" class="w-7 h-7 rounded flex items-center justify-center bg-red-600 hover:bg-red-500 text-white shadow-md transition-transform active:scale-90" title="Delete show from monitoring">
                  <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2.2"><path stroke-linecap="round" stroke-linejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/></svg>
                </button>
              </div>
            </div>
          </div>

          <div class="p-2.5 flex flex-col justify-between bg-[#18181c] min-h-[5.5rem]">
            <h3 class="text-[13px] font-semibold text-zinc-100 leading-[1.4] line-clamp-2 min-h-[2.55rem] overflow-hidden pb-[1px]" title="${show.display_name}">
              ${show.display_name}
            </h3>

            <div class="pt-2 border-t border-[#222228] flex items-center justify-between gap-2.5 text-xs font-mono">
              <span class="truncate text-zinc-400 font-medium min-w-0" title="${feedName}">${feedName}</span>
              ${versionLabel}
              <span class="show-countdown flex-shrink-0 text-zinc-200 font-semibold ml-auto" ${countdownAttr} title="${show.next_airing_formatted ? show.next_airing_formatted : ''}">${airInfo}</span>
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
          <div class="pb-2 mb-2 border-b ${isPastDay ? 'border-[#22222a]' : 'border-[#26262e]'}">
            <div class="flex items-baseline gap-1.5 ${isPastDay ? 'opacity-80' : 'opacity-100'}">
              <span class="text-sm font-bold ${isPastDay ? 'text-zinc-400' : 'text-zinc-200'}">${dayName}</span>
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
                  <span class="w-2 h-2 rounded-full bg-sky-400/80 absolute -left-[4px]"></span>
                  <div class="h-[1.5px] w-full bg-gradient-to-r from-sky-500/60 via-sky-500/30 to-transparent"></div>
                </div>
                <div class="text-zinc-600 text-xs font-mono">No releases</div>
              </div>
            `;
          } else {
            itemsHtml = `<div class="py-6 text-zinc-600 text-xs font-mono select-none">No releases</div>`;
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
                  <span class="w-2 h-2 rounded-full bg-sky-400/80 absolute -left-[4px]"></span>
                  <div class="h-[1.5px] w-full bg-gradient-to-r from-sky-500/60 via-sky-500/30 to-transparent"></div>
                </div>
              `;
              renderedLine = true;
            }

            itemsHtml += createTimeSlotHtml(slot, isToday);
          });

          if (isToday && !renderedLine) {
            itemsHtml += `
              <div class="relative py-1 my-1.5 -ml-3 flex items-center gap-1 z-20 select-none">
                <span class="w-2 h-2 rounded-full bg-sky-400/80 absolute -left-[4px]"></span>
                <div class="h-[1.5px] w-full bg-gradient-to-r from-sky-500/60 via-sky-500/30 to-transparent"></div>
              </div>
            `;
          }
        }

        gridHtml += `
          <div class="flex flex-col min-h-[380px] ${isPastDay ? 'opacity-85' : 'opacity-100'}">
            ${headerHtml}
            <div class="relative pl-3.5 space-y-4 flex-1 flex flex-col before:absolute before:left-[3px] before:top-2 before:bottom-2 before:w-[1.5px] before:bg-[#282834]">
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
          ? `<img src="${show.cover_image}" alt="${show.display_name}" class="w-16 aspect-[2/3] rounded-md object-cover flex-shrink-0 bg-[#121215] shadow-md ${isPaused ? 'grayscale' : ''}" loading="lazy" onerror="this.onerror=null;this.src='https://via.placeholder.com/100x140/1a1a20/4a4a58?text=Poster'">`
          : `<div class="w-16 aspect-[2/3] bg-[#141418] border border-zinc-800 rounded-md flex items-center justify-center text-[10px] font-mono text-zinc-500 flex-shrink-0">No Art</div>`;

        let epText = show.next_airing_episode ? `EP${show.next_airing_episode}` : (show.last_confirmed_episode ? `EP${show.last_confirmed_episode}` : 'EP1');
        const anilistUrl = show.anilist_id ? `https://anilist.co/anime/${show.anilist_id}` : '#';

        return `
          <div class="relative group select-none">
            <div class="flex items-center justify-between gap-1.5 mb-1.5">
              <div class="flex items-center gap-1.5">
                <span class="w-3 h-3 rounded-full ${dotColor} absolute -left-[15px] z-10"></span>
                <span class="text-[13px] font-mono ${isPaused ? 'text-zinc-400' : (hasPassed ? 'text-zinc-400' : 'text-zinc-100')} font-bold tracking-tight">${slot.timeStr}</span>
              </div>
              <span class="text-xs font-mono font-semibold ${isPaused ? 'text-zinc-500' : (hasPassed ? 'text-zinc-500' : 'text-zinc-400')}">${epText}</span>
            </div>

            <a href="${anilistUrl}" target="_blank" rel="noopener noreferrer" class="flex gap-3 items-start cursor-pointer transition-opacity ${isPaused ? 'grayscale opacity-50 hover:opacity-80' : (hasPassed ? 'opacity-80 hover:opacity-100' : 'opacity-100 hover:opacity-90')}">
              ${posterImg}

              <div class="flex-1 min-w-0 pt-0.5">
                <h4 class="text-sm font-semibold leading-snug line-clamp-3 ${titleColor}" title="${show.display_name}">
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
          ? `<img src="${show.cover_image}" alt="${show.display_name}" class="w-16 aspect-[2/3] rounded-md object-cover flex-shrink-0 bg-[#121215] shadow-md ${isPaused ? 'grayscale' : ''}" loading="lazy" onerror="this.onerror=null;this.src='https://via.placeholder.com/100x140/1a1a20/4a4a58?text=Poster'">`
          : `<div class="w-16 aspect-[2/3] bg-[#141418] border border-zinc-800 rounded-md flex items-center justify-center text-[10px] font-mono text-zinc-500 flex-shrink-0">No Art</div>`;

        let epText = show.next_airing_episode ? `EP${show.next_airing_episode}` : (show.last_confirmed_episode ? `EP${show.last_confirmed_episode}` : 'EP1');
        const anilistUrl = show.anilist_id ? `https://anilist.co/anime/${show.anilist_id}` : '#';

        return `
          <a href="${anilistUrl}" target="_blank" rel="noopener noreferrer" class="flex gap-3 items-start cursor-pointer transition-opacity ${isPaused ? 'grayscale opacity-50 hover:opacity-80' : (hasPassed ? 'opacity-80 hover:opacity-100' : 'opacity-100 hover:opacity-90')}">
            ${posterImg}

            <div class="flex-1 min-w-0 pt-0.5">
              <div class="flex items-start justify-between gap-1">
                <h4 class="text-sm font-semibold leading-snug line-clamp-3 ${titleColor}" title="${show.display_name}">
                  ${show.display_name}
                </h4>
                <span class="text-xs font-mono font-semibold ${isPaused ? 'text-zinc-500' : (hasPassed ? 'text-zinc-500' : 'text-zinc-400')} shrink-0 ml-1">${epText}</span>
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
              <span class="text-[13px] font-mono ${slot.hasPassed ? 'text-zinc-400' : 'text-zinc-100'} font-bold tracking-tight">${slot.timeStr}</span>
            </div>
          </div>

          <div class="space-y-3">
            ${showsHtml}
          </div>
        </div>
      `;
    }

    async function viewShowRule(showId) {
      currentInspectedShowId = showId;
      const modal = document.getElementById('rule-modal');
      const titleEl = document.getElementById('rule-modal-title');
      const ruleNameEl = document.getElementById('rule-modal-rule-name');
      const contentEl = document.getElementById('rule-modal-content');

      contentEl.innerHTML = '<div class="text-center py-8 text-zinc-500 font-mono">Loading show details...</div>';
      modal.classList.remove('hidden');

      try {
        // The rule panel and the episode ledger are independent, so fetch both.
        const [res, episodeRes] = await Promise.all([
          fetch(`/api/shows/${showId}/rule`),
          fetch(`/api/shows/${showId}/episodes`)
        ]);
        const data = await res.json();
        const episodes = episodeRes.ok ? await episodeRes.json() : [];

        const isCompleted = (data.status === 'completed');
        const isPaused = (data.status === 'paused');
        const isRuleActive = data.enabled === true;
        const isDirect = data.download_mode === 'direct';

        titleEl.textContent = data.display_name;
        ruleNameEl.textContent = isDirect ? 'Direct download engine' : (data.rule_name || (isCompleted ? 'Completed Series' : 'No Rule Configured'));

        let ruleStatusText = '';
        if (isDirect) {
          ruleStatusText = '<span class="px-2.5 py-0.5 rounded-md text-xs font-bold bg-sky-950/80 text-sky-300 border border-sky-800 flex items-center gap-1.5"><span class="w-1.5 h-1.5 rounded-full bg-sky-400"></span>Direct Engine Active</span>';
        } else if (isCompleted) {
          ruleStatusText = '<span class="px-2.5 py-0.5 rounded-md text-xs font-bold bg-indigo-950/80 text-indigo-300 border border-indigo-800 flex items-center gap-1.5"><span class="w-1.5 h-1.5 rounded-full bg-indigo-400"></span>Completed</span>';
        } else if (isPaused) {
          ruleStatusText = '<span class="text-[11px] font-medium text-zinc-400">Paused</span>';
        } else if (isRuleActive) {
          ruleStatusText = '<span class="text-[11px] font-medium text-zinc-300">Enabled</span>';
        } else {
          ruleStatusText = '<span class="text-[11px] font-medium text-zinc-500">Waiting for air date</span>';
        }

        const isUpcoming = data.is_upcoming === true;
        const isTesting = data.status === 'unconfirmed' && !isUpcoming && !data.feed_pinned;

        // No release matched and no feed pinned: the feed is not decided, so the
        // selector offers Auto-discover and only marks the default being probed.
        const isAutoManaged = !isCompleted && !data.feed_pinned && !data.has_learned_pattern;
        const selectedFeedId = isAutoManaged ? 0 : (data.current_feed_id || 0);
        const feedLabel = isUpcoming ? 'Feed (not decided yet)' : isTesting ? 'Feed (auto)' : 'Assigned Feed';

        const feedOptions = `<option value="0">[ Auto-discover ]</option>` +
          allFeeds.map(f => `<option value="${f.id}" ${f.id === selectedFeedId ? 'selected' : ''}>#${f.priority} ${escapeHtml(f.qbit_feed_name)}</option>`).join('');

        // Until a release has been matched, the feed is only a starting point:
        // say what it actually means instead of implying it was chosen.
        let feedHint = '';
        if (!isCompleted && data.has_rule) {
            if (isUpcoming) {
                feedHint = `
                  <p class="text-[11px] text-zinc-500 leading-snug">Auto-discover is on. No release seen yet, so nothing is decided — every feed is checked and the rule moves by itself to whichever one posts the episode first.</p>
                `;
            } else if (isTesting) {
                feedHint = `
                  <p class="text-[11px] text-zinc-500 leading-snug">Auto-discover is on and still testing: every feed is re-checked in priority order, so this rule moves by itself if another feed posts the release first.</p>
                `;
            }
        }

        const countMatched = (data.matched_articles || []).length;
        const matchCountLabel = countMatched === 1 ? '1 match' : `${countMatched} matches`;
        const listRows = (items, emptyText) => (items && items.length > 0)
          ? items.map(a => `
              <li class="text-[11px] font-mono text-zinc-400 py-1.5 border-t border-[#1c1c22] first:border-t-0 first:pt-0 flex items-center gap-2">
                <span class="truncate select-all flex-1" title="${escapeHtml(a)}">${escapeHtml(a)}</span>
                <button type="button" data-title="${escapeHtml(a)}" onclick="quickDownloadMatch(${showId}, this)"
                  class="shrink-0 w-6 h-6 rounded flex items-center justify-center bg-[#1e1e26] hover:bg-emerald-600 border border-[#2e2e38] hover:border-emerald-500 text-zinc-400 hover:text-white transition-colors active:scale-90"
                  title="Download this release now with the same save path, category and ratio as the rule">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" class="w-3.5 h-3.5"><path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/></svg>
                </button>
              </li>
            `).join('')
          : `<li class="text-[11px] font-mono text-zinc-600 py-1.5">${emptyText}</li>`;
        const articlesHtml = listRows(data.matched_articles, 'No cached RSS articles currently match this rule pattern.');

        const candidateBanner = data.candidate_feed_id ? `
          <p class="text-[11px] text-zinc-500 leading-snug">Watching <span class="text-zinc-300">${escapeHtml(data.candidate_feed_name || 'another feed')}</span> — a release for this show appeared there but not on the preferred feed. The rule moves automatically if that stays true for 5 minutes.</p>
        ` : '';

        const pinnedBanner = data.feed_pinned ? `
          <p class="text-[11px] text-zinc-500 leading-snug">Pinned by you — auto-detect will not move this show to another feed.</p>
        ` : '';

        const noRulePlaceholder = isCompleted ? `
          <div class="bg-[#121215] border border-emerald-950/60 rounded-lg p-3 text-center">
            <div class="text-xs font-semibold text-emerald-400">Completed Series</div>
            <p class="text-[11px] text-zinc-400 mt-0.5">All episodes have aired and were confirmed downloaded. RSS rule has been disabled.</p>
          </div>
        ` : `
          <div class="bg-[#121215] border border-[#2a2a34] rounded-lg p-3 text-center">
            <div class="text-xs font-semibold text-sky-400">Upcoming Show (Awaiting Air Date)</div>
            <p class="text-[11px] text-zinc-400 mt-0.5">The supervisor automatically discovers and arms the verified qBittorrent rule when the episode drops.</p>
          </div>
        `;

        const regexSections = data.has_rule ? `
          <div class="space-y-1">
            <label for="modal-must-contain" class="block text-[11px] font-bold uppercase tracking-wider text-zinc-300">Must Contain (Regex Filter)</label>
            <input type="text" id="modal-must-contain" value="${data.must_contain || ''}" placeholder=".*" class="w-full bg-[#121215] border border-[#30303a] rounded-lg px-3.5 py-2 text-xs sm:text-sm font-mono text-emerald-400 focus:outline-none focus:border-zinc-500 shadow-inner select-all">
          </div>

          <div class="space-y-1">
            <label for="modal-must-not-contain" class="block text-[11px] font-bold uppercase tracking-wider text-zinc-400">Must Not Contain Filter</label>
            <input type="text" id="modal-must-not-contain" value="${data.must_not_contain || ''}" placeholder="(720p|480p|...)" class="w-full bg-[#121215] border border-[#30303a] rounded-lg px-3.5 py-2 text-xs sm:text-sm font-mono text-zinc-300 focus:outline-none focus:border-zinc-500 shadow-inner select-all">
          </div>
        ` : noRulePlaceholder;

        const articlesSection = (data.has_rule && data.has_learned_pattern) ? `
          <div class="space-y-1 pt-0.5">
            <div class="flex items-baseline justify-between gap-2">
              <span class="text-[11px] font-bold uppercase tracking-wider text-zinc-300">Currently Matching in RSS Feed</span>
              <span class="text-[11px] font-mono text-zinc-500">${matchCountLabel}</span>
            </div>
            <ul class="bg-[#121215] border border-[#2e2e38] rounded-lg px-3 py-2 max-h-24 overflow-y-auto">
              ${articlesHtml}
            </ul>
          </div>
        ` : '';

        contentEl.innerHTML = `
          <div class="space-y-1">
            <div class="flex items-center justify-between">
              <label for="modal-feed-id" class="block text-[11px] font-bold uppercase tracking-wider text-zinc-300">${feedLabel}</label>
              ${ruleStatusText}
            </div>
            <select id="modal-feed-id" class="w-full bg-[#121215] border border-[#30303a] rounded-lg px-3.5 py-2 text-xs sm:text-sm text-zinc-100 font-medium focus:outline-none focus:border-zinc-500 shadow-inner">
              ${feedOptions}
            </select>
            ${(data.feed_url && !isAutoManaged) ? `<div class="text-[11px] text-zinc-500 font-mono truncate px-0.5">${data.feed_url}</div>` : ''}
            ${feedHint}
            ${pinnedBanner}
            ${candidateBanner}
          </div>

          ${regexSections}

          <div class="space-y-1">
            <label for="modal-save-path" class="block text-[11px] font-bold uppercase tracking-wider text-zinc-300">Save Path</label>
            <input type="text" id="modal-save-path" value="${data.save_path || data.save_folder || ''}" placeholder="~/Anime/${data.display_name}" class="w-full bg-[#121215] border border-[#30303a] rounded-lg px-3.5 py-2 text-xs sm:text-sm font-mono text-zinc-100 focus:outline-none focus:border-zinc-500 shadow-inner" title="${data.save_path || ''}">
          </div>

          <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div class="space-y-1">
              <label for="modal-category" class="block text-[11px] font-bold uppercase tracking-wider text-zinc-300">Category</label>
              <input type="text" id="modal-category" value="${data.category || ''}" placeholder="${currentSettings.default_category || 'anime'}" class="w-full bg-[#121215] border border-[#30303a] rounded-lg px-3.5 py-2 text-xs sm:text-sm font-mono text-zinc-100 focus:outline-none focus:border-zinc-500 shadow-inner">
            </div>
            <div class="space-y-1">
              <label for="modal-ratio-limit" class="block text-[11px] font-bold uppercase tracking-wider text-zinc-300">Seed Ratio Limit</label>
              <input type="number" step="0.1" min="0" id="modal-ratio-limit" value="${data.ratio_limit !== undefined && data.ratio_limit !== null ? data.ratio_limit : ''}" placeholder="${currentSettings.default_seed_ratio || 1.0}" class="w-full bg-[#121215] border border-[#30303a] rounded-lg px-3.5 py-2 text-xs sm:text-sm font-mono text-zinc-100 focus:outline-none focus:border-zinc-500 shadow-inner">
            </div>
          </div>

        const episodeSection = isDirect ? `
          <div class="space-y-1 pt-1">
            <div class="flex items-center justify-between">
              <span class="text-[11px] font-bold uppercase tracking-wider text-zinc-300">Episode Ownership</span>
              <span class="text-[11px] font-mono text-zinc-500">${episodes.length} tracked</span>
            </div>
            <div class="bg-[#121215] border border-[#2e2e38] rounded-lg p-2 max-h-40 overflow-y-auto space-y-1">
              ${episodes.length ? episodes.map(ep => `
                <div class="flex items-center justify-between gap-2 text-[11px] font-mono px-2 py-1 rounded ${ep.version > 1 ? 'bg-amber-950/50 text-amber-300' : 'text-zinc-400'}">
                  <span>Ep ${ep.episode_number}${ep.version > 1 ? ` · v${ep.version}` : ''}</span>
                  <span class="truncate">${escapeHtml(ep.status)}${ep.last_error ? ` · ${escapeHtml(ep.last_error)}` : ''}</span>
                </div>
              `).join('') : '<div class="text-zinc-500 text-xs text-center py-2">No episode records yet.</div>'}
            </div>
          </div>
        ` : '';

          ${articlesSection}
          ${episodeSection}
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
        };
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

      const currentFeedId = feedSelect ? parseInt(feedSelect.value) : 0;
      const currentSaveFolder = savePathInput ? savePathInput.value.trim() : '';
      const currentCategory = categoryInput ? categoryInput.value.trim() : '';
      const currentRatio = ratioInput && ratioInput.value !== '' ? parseFloat(ratioInput.value) : undefined;
      const currentMustContain = mustContainInput ? mustContainInput.value.trim() : '';
      const currentMustNotContain = mustNotContainInput ? mustNotContainInput.value.trim() : '';

      const hasChanged = !modalInitialState || (
        currentFeedId !== modalInitialState.current_feed_id ||
        currentSaveFolder !== modalInitialState.save_folder ||
        currentCategory !== modalInitialState.category ||
        currentRatio !== modalInitialState.ratio_limit ||
        currentMustContain !== modalInitialState.must_contain ||
        currentMustNotContain !== modalInitialState.must_not_contain
      );

      if (!hasChanged) {
        closeRuleModal();
        return;
      }

      const btn = document.getElementById('btn-save-show-modal');
      btn.disabled = true;
      btn.textContent = 'Saving...';

      const isRegexChanged = modalInitialState && currentMustContain !== modalInitialState.must_contain;
      const isMustNotChanged = modalInitialState && currentMustNotContain !== modalInitialState.must_not_contain;

      const payload = {
        // Only sent when the feed was actually changed: an explicit pick pins the
        // feed, while saving other fields must leave auto-detect in charge.
        current_feed_id: !modalInitialState || currentFeedId !== modalInitialState.current_feed_id ? currentFeedId : undefined,
        save_folder: currentSaveFolder || undefined,
        category: currentCategory || undefined,
        ratio_limit: currentRatio,
        must_contain: isRegexChanged ? currentMustContain : undefined,
        must_not_contain: isMustNotChanged ? currentMustNotContain : undefined,
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
      closeRuleModal();
      await rediscoverShow(showId);
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

    async function rediscoverShow(id) {
      try {
        const data = await apiFetch(`/api/shows/${id}/rediscover`, { method: 'POST' });
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
      e.currentTarget.classList.add('opacity-40', 'bg-[#22222a]');
    }

    function handleDragOver(e) {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      e.currentTarget.classList.add('border-t-2', 'border-sky-500', 'bg-[#1c1c24]');
    }

    function handleDragLeave(e) {
      e.currentTarget.classList.remove('border-t-2', 'border-sky-500', 'bg-[#1c1c24]');
    }

    function handleDragEnd(e) {
      e.currentTarget.classList.remove('opacity-40', 'bg-[#22222a]');
      document.querySelectorAll('#feeds-table-body tr').forEach(r => {
        r.classList.remove('border-t-2', 'border-sky-500', 'bg-[#1c1c24]');
      });
    }

    async function handleDrop(e, targetIndex) {
      e.preventDefault();
      e.currentTarget.classList.remove('border-t-2', 'border-sky-500', 'bg-[#1c1c24]');
      if (dragSourceIndex === null || dragSourceIndex === targetIndex) return;

      const movedItem = allFeeds.splice(dragSourceIndex, 1)[0];
      allFeeds.splice(targetIndex, 0, movedItem);

      allFeeds.forEach((f, idx) => { f.priority = idx + 1; });
      renderFeedsTable();

      const reorderPayload = {
        feeds: allFeeds.map((f, idx) => ({ id: f.id, priority: idx + 1 }))
      };

      try {
        const res = await fetch('/api/feeds/reorder', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(reorderPayload)
        });
        const data = await res.json();
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
        const res = await fetch('/api/feeds');
        allFeeds = await res.json();
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
            class="hover:bg-[#1e1e24] cursor-grab active:cursor-grabbing transition-colors group select-none">
          <td class="py-3.5 px-3 text-center text-zinc-500 group-hover:text-zinc-300">
            <svg class="w-4 h-4 mx-auto opacity-60 group-hover:opacity-100 transition-opacity" fill="currentColor" viewBox="0 0 24 24"><path d="M9 5a2 2 0 11-4 0 2 2 0 014 0zm0 7a2 2 0 11-4 0 2 2 0 014 0zm0 7a2 2 0 11-4 0 2 2 0 014 0zm10-14a2 2 0 11-4 0 2 2 0 014 0zm0 7a2 2 0 11-4 0 2 2 0 014 0zm0 7a2 2 0 11-4 0 2 2 0 014 0z"/></svg>
          </td>
          <td class="py-3.5 px-3 font-mono font-bold text-zinc-300 text-sm">#${f.priority}</td>
          <td class="py-3.5 px-4">
            <div class="font-semibold text-zinc-100 text-sm">${f.qbit_feed_name}</div>
            <div class="text-xs text-zinc-500 font-mono truncate max-w-2xl mt-0.5">${f.qbit_feed_url}</div>
          </td>
        </tr>
      `).join('') || '<tr><td colspan="3" class="text-center py-8 text-zinc-500 font-mono">No feeds registered.</td></tr>';
    }

    async function syncFeeds() {
      try {
        const res = await fetch('/api/feeds/sync', { method: 'POST' });
        const data = await res.json();
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
          btnEn.className = 'px-2.5 py-1 font-semibold rounded transition-colors text-sky-400 bg-[#262632] shadow-sm';
          btnJa.className = 'px-2.5 py-1 font-medium rounded transition-colors text-zinc-400 hover:text-zinc-200';
        } else {
          btnJa.className = 'px-2.5 py-1 font-semibold rounded transition-colors text-sky-400 bg-[#262632] shadow-sm';
          btnEn.className = 'px-2.5 py-1 font-medium rounded transition-colors text-zinc-400 hover:text-zinc-200';
        }
      }
    }

    function updateDownloadModeUi(mode) {
      const selected = mode || 'rules';
      const hidden = document.getElementById('set-download-mode');
      if (hidden) hidden.value = selected;

      ['rules', 'observe', 'direct'].forEach(name => {
        const button = document.getElementById(`btn-mode-${name}`);
        if (!button) return;
        const active = name === selected;
        button.className = active
          ? 'px-2.5 py-1 font-semibold rounded transition-colors text-sky-400 bg-[#262632] shadow-sm'
          : 'px-2.5 py-1 font-medium rounded transition-colors text-zinc-400 hover:text-zinc-200';
      });
    }

    async function setDownloadMode(mode) {
      if (!['rules', 'observe', 'direct'].includes(mode)) return;
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
      updateTitleLanguageUi(lang);
      try {
        await fetch('/api/settings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title_language: lang })
        });
        await loadShows();
      } catch (err) {
        console.error('Failed to update title language:', err);
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

    async function syncAniListNow() {
      const statusEl = document.getElementById('sync-anilist-status');
      statusEl.textContent = 'Syncing...';
      statusEl.className = 'text-xs font-mono text-zinc-400';
      try {
        const res = await fetch('/api/settings/sync-anilist', { method: 'POST' });
        const data = await res.json();
        statusEl.textContent = data.message || 'Synced.';
        statusEl.className = 'text-xs font-mono text-emerald-400 font-semibold';
        showToast(data.message || 'AniList synced.', 'success');
        loadShows();
      } catch (err) {
        statusEl.textContent = `Failed: ${err}`;
        statusEl.className = 'text-xs font-mono text-rose-400';
        showToast(`AniList sync failed: ${err}`, 'error');
      }
    }

    async function clearAllShows() {
      if (!confirm('Delete ALL monitored shows and remove all qBittorrent RSS rules?')) return;
      try {
        const res = await fetch('/api/settings/clear-all', { method: 'POST' });
        const data = await res.json();
        showToast(data.message, 'success');
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
      text.textContent = 'Checking...';

      try {
        const data = await apiFetch('/api/cycle/run', { method: 'POST' });
        showToast(data.message || 'Check completed.', 'success');
        loadShows();
        updateStatus(true);
      } catch (err) {
        // A busy cycle is a conflict, not a failure: something is already
        // running, so report it as information.
        if (err.status === 409) {
          showToast(err.message || 'A supervision cycle is already in progress.', 'info');
        } else {
          showToast(`Check error: ${err.message || err}`, 'error');
        }
      } finally {
        btn.disabled = false;
        spinner.classList.add('hidden');
        text.textContent = 'Check for New Episodes';
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

    function renderHistory() {
      const container = document.getElementById('history-container');
      const badge = document.getElementById('badge-total-history');
      if (!container) return;

      if (badge) badge.textContent = `${cachedHistory.length}`;

      if (!cachedHistory || cachedHistory.length === 0) {
        container.innerHTML = `
          <div class="bg-[#18181c] border border-[#26262e] rounded-2xl p-8 text-center space-y-2">
            <div class="text-zinc-400 font-medium text-sm">No matches recorded yet</div>
            <p class="text-xs text-zinc-500 max-w-sm mx-auto">When an RSS rule matches a release in qBittorrent, it will appear here as confirmation that it began downloading.</p>
          </div>
        `;
        return;
      }

      const rows = cachedHistory.map(h => {
        const relTime = formatRelativeTime(h.created_at);
        const fullTime = h.created_at ? new Date(h.created_at).toLocaleString() : '';
        const regexVal = h.matched_regex || '';

        const ruleBadge = regexVal ? `
          <span class="relative group/regex inline-block">
            <span class="font-mono font-medium text-zinc-100 bg-[#22222a] px-1.5 py-0.5 rounded-md border border-[#2f2f3b] cursor-help hover:border-emerald-500/50 hover:text-white transition-colors">
              ${escapeHtml(h.rule_name || '')}
            </span>
            <span class="pointer-events-none absolute bottom-full left-1/2 -translate-x-1/2 mb-2 hidden group-hover/regex:flex flex-col items-center z-50">
              <span class="bg-[#121216] text-emerald-300 border border-[#30303c] text-xs font-mono font-medium px-2.5 py-1 rounded-lg shadow-2xl whitespace-nowrap max-w-sm sm:max-w-md truncate">
                ${escapeHtml(regexVal)}
              </span>
              <span class="w-2 h-2 bg-[#121216] border-r border-b border-[#30303c] rotate-45 -mt-1"></span>
            </span>
          </span>
        ` : `
          <span class="font-mono font-medium text-zinc-100 bg-[#22222a] px-1.5 py-0.5 rounded-md border border-[#2f2f3b]">
            ${escapeHtml(h.rule_name || '')}
          </span>
        `;

        return `
          <div class="bg-[#18181c] border border-[#26262e] hover:border-[#383844] rounded-2xl p-3.5 sm:px-4 sm:py-3 flex items-start sm:items-center justify-between gap-3 transition-colors shadow-sm">
            <div class="flex items-start sm:items-center gap-3 min-w-0">
              <span class="text-emerald-400 font-bold text-sm select-none shrink-0 mt-0.5 sm:mt-0">✓</span>
              <div class="text-xs sm:text-sm text-zinc-300 leading-snug break-all sm:break-normal">
                Rule ${ruleBadge} matched <span class="font-mono text-emerald-300 font-medium select-all">${escapeHtml(h.release_title || '')}</span>
              </div>
            </div>
            <span class="text-xs text-zinc-500 font-mono flex-shrink-0 whitespace-nowrap self-start sm:self-center" title="${escapeHtml(fullTime)}">${relTime}</span>
          </div>
        `;
      });

      container.innerHTML = rows.join('');
    }

    async function clearHistory() {
      if (!confirm('Clear all match history?')) return;
      try {
        await fetch('/api/history', { method: 'DELETE' });
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
        let levelBadge = '<span class="text-zinc-400 font-semibold">[INFO]</span>';
        if (level === 'ERROR') levelBadge = '<span class="text-rose-400 font-semibold">[ERROR]</span>';
        else if (level === 'WARNING' || level === 'WARN') levelBadge = '<span class="text-amber-400 font-semibold">[WARN]</span>';
        else if (level === 'DEBUG') levelBadge = '<span class="text-sky-400 font-semibold">[DEBUG]</span>';

        const timeStr = l.time_str || (l.timestamp ? new Date(l.timestamp).toLocaleTimeString() : '');

        return `<div class="flex items-start gap-2.5 py-0.5 hover:bg-[#15151c] px-1.5 rounded transition-colors leading-relaxed">
          <span class="text-zinc-500 select-none text-[11px] font-mono shrink-0">${timeStr}</span>
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
