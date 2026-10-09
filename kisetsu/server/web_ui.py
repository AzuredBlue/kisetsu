from typing import Dict, Optional

from fastapi.responses import HTMLResponse

def get_web_ui_html(headers: Optional[Dict[str, str]] = None) -> HTMLResponse:
    html = """<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Kisetsu</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
  <script>
    // Resolve the theme before first paint. 'system' follows the OS.
    (function () {
      var mode = 'dark';
      try { mode = localStorage.getItem('kisetsu_theme') || 'dark'; } catch (err) {}
      var light = mode === 'light' || (mode === 'system' && window.matchMedia('(prefers-color-scheme: light)').matches);
      document.documentElement.setAttribute('data-theme', light ? 'light' : 'dark');
    })();
  </script>
  <script src="https://cdn.tailwindcss.com"></script>
  <script>
    tailwind.config = {
      theme: {
        extend: {
          fontFamily: {
            sans: ['IBM Plex Sans', 'ui-sans-serif', 'system-ui', '-apple-system', 'Segoe UI', 'Roboto', 'sans-serif'],
            mono: ['IBM Plex Mono', 'ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
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
            'line-hover': 'rgb(var(--line-hover) / <alpha-value>)',
            'line-strong': 'rgb(var(--line-strong) / <alpha-value>)',
            accent: { DEFAULT: 'rgb(var(--ac) / <alpha-value>)', strong: 'rgb(var(--ac-strong) / <alpha-value>)', soft: 'rgb(var(--ac-soft) / <alpha-value>)' },
            accent2: 'rgb(var(--ac2) / <alpha-value>)',
            zinc: Object.fromEntries([50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950].map(n => [n, `rgb(var(--z${n}) / <alpha-value>)`])),
            ...Object.fromEntries(['emerald', 'amber', 'rose', 'sky', 'violet'].map(f => [f, Object.fromEntries([3, 4, 5].map(n => [n * 100, `rgb(var(--${f}-${n}) / <alpha-value>)`]))])),
          },
          borderRadius: { xl: '0.875rem', '2xl': '1.125rem' },
        },
      },
    };
  </script>
  <style>
    * { scrollbar-width: thin; scrollbar-color: rgb(var(--line)) transparent; }
    /* Theme tokens. Dark is the default; light overrides below. Tailwind's zinc and the
       status hues are mapped onto these variables, so text-zinc-400, text-rose-400 and
       friends flip with the theme without touching the markup. */
    :root {
      color-scheme: dark;
      --ac-art: 45 212 191; --ac: 45 212 191; --ac-soft: 94 234 212; --ac-strong: 20 184 166; --ac-ink: 4 32 30; --ac2: 56 189 248;
      --bg: 19 19 23; --sunken: 15 15 19; --chrome: 23 23 28; --surface: 27 27 33; --field: 19 19 23; --raised: 35 35 43; --raised2: 43 43 53; --line: 47 47 58; --line-soft: 37 37 45; --line-hover: 58 58 72; --line-strong: 74 74 90;
      --z50: 250 250 250; --z100: 244 244 245; --z200: 228 228 231; --z300: 212 212 216; --z400: 161 161 170; --z500: 113 113 122; --z600: 82 82 91; --z700: 63 63 70; --z800: 39 39 42; --z900: 24 24 27; --z950: 9 9 11;
      --emerald-3: 110 231 183; --emerald-4: 52 211 153; --emerald-5: 16 185 129;
      --amber-3: 252 211 77; --amber-4: 251 191 36; --amber-5: 245 158 11;
      --rose-3: 253 164 175; --rose-4: 251 113 133; --rose-5: 244 63 94;
      --sky-3: 125 211 252; --sky-4: 56 189 248; --sky-5: 14 165 233;
      --violet-3: 196 181 253; --violet-4: 167 139 250; --violet-5: 139 92 246;
      --t-label: 212 212 216; --t-hint: 139 139 151; --t-field: 244 244 245; --t-ph: 93 93 107; --t-btn: 228 228 231; --t-seg: 154 154 166; --t-title: 250 250 250;
      --danger-bg: 42 24 24; --danger-bd: 74 36 36; --danger-fg: 240 134 139; --danger-bg-h: 56 29 29; --danger-bd-h: 94 43 43;
    }
    :root[data-theme="light"] {
      color-scheme: light;
      --ac: 13 148 136; --ac-soft: 15 118 110; --ac-strong: 15 118 110; --ac-ink: 255 255 255; --ac2: 2 132 199;
      --bg: 243 242 238; --sunken: 230 229 224; --chrome: 234 233 228; --surface: 250 249 246; --field: 253 252 250; --raised: 238 237 232; --raised2: 228 227 221; --line: 219 217 211; --line-soft: 230 228 223; --line-hover: 200 198 191; --line-strong: 172 170 162;
      --z50: 24 24 27; --z100: 39 39 42; --z200: 52 52 58; --z300: 63 63 70; --z400: 90 90 99; --z500: 108 108 118; --z600: 140 140 150; --z700: 190 190 196; --z800: 212 212 216; --z900: 228 228 231; --z950: 240 240 242;
      --emerald-3: 6 95 70; --emerald-4: 4 120 87; --emerald-5: 5 150 105;
      --amber-3: 146 64 14; --amber-4: 180 83 9; --amber-5: 217 119 6;
      --rose-3: 159 18 57; --rose-4: 190 18 60; --rose-5: 225 29 72;
      --sky-3: 7 89 133; --sky-4: 3 105 161; --sky-5: 2 132 199;
      --violet-3: 91 33 182; --violet-4: 109 40 217; --violet-5: 124 58 237;
      --t-label: 63 63 70; --t-hint: 108 108 118; --t-field: 24 24 27; --t-ph: 150 150 158; --t-btn: 39 39 42; --t-seg: 90 90 99; --t-title: 17 17 20;
      --danger-bg: 253 236 236; --danger-bd: 240 190 190; --danger-fg: 176 30 50; --danger-bg-h: 251 224 224; --danger-bd-h: 232 165 165;
    }
    /* Anything drawn over cover art stays light-on-dark in both themes. */
    .on-art {
      --z50: 250 250 250; --z100: 244 244 245; --z200: 228 228 231; --z300: 212 212 216; --z400: 161 161 170; --z500: 113 113 122; --z600: 82 82 91; --z700: 63 63 70; --z800: 39 39 42; --z900: 24 24 27; --z950: 9 9 11;
      --emerald-3: 110 231 183; --emerald-4: 52 211 153; --emerald-5: 16 185 129;
      --amber-3: 252 211 77; --amber-4: 251 191 36; --amber-5: 245 158 11;
      --rose-3: 253 164 175; --rose-4: 251 113 133; --rose-5: 244 63 94;
      --sky-3: 125 211 252; --sky-4: 56 189 248; --sky-5: 14 165 233;
      --violet-3: 196 181 253; --violet-4: 167 139 250; --violet-5: 139 92 246;
    }
    /* Status pills: tinted, no dot. */
    .st-ok { background: rgb(6 43 32); color: rgb(79 177 136); border-color: rgb(15 81 56); }
    .st-warn { background: rgb(43 34 8); color: rgb(194 160 63); border-color: rgb(85 67 10); }
    .st-info { background: rgb(16 26 61); color: rgb(108 147 201); border-color: rgb(31 58 107); }
    .st-bad { background: rgb(43 17 17); color: rgb(194 106 106); border-color: rgb(94 35 35); }
    .st-done { background: rgb(34 29 51); color: rgb(169 155 208); border-color: rgb(61 51 96); }
    .st-idle { background: rgb(var(--raised)); color: rgb(145 145 154); border-color: rgb(var(--line)); }
    :root[data-theme="light"] .st-ok { background: rgb(222 241 231); color: rgb(22 101 70); border-color: rgb(168 212 190); }
    :root[data-theme="light"] .st-warn { background: rgb(250 238 208); color: rgb(133 77 14); border-color: rgb(232 205 140); }
    :root[data-theme="light"] .st-info { background: rgb(220 234 250); color: rgb(29 78 140); border-color: rgb(160 190 230); }
    :root[data-theme="light"] .st-bad { background: rgb(250 226 226); color: rgb(160 40 50); border-color: rgb(235 180 180); }
    :root[data-theme="light"] .st-done { background: rgb(235 228 248); color: rgb(98 62 160); border-color: rgb(200 185 230); }
    :root[data-theme="light"] .st-idle { color: rgb(100 100 110); }
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
    .field-label { display: block; font-size: 0.8125rem; font-weight: 500; color: rgb(var(--t-label)); margin-bottom: 0.375rem; }
    .field-hint { font-size: 0.75rem; line-height: 1.45; color: rgb(var(--t-hint)); margin-top: 0.375rem; }
    .field {
      width: 100%; background: rgb(var(--field)); border: 1px solid rgb(var(--line)); border-radius: 0.625rem;
      padding: 0.55rem 0.8rem; font-size: 0.875rem; color: rgb(var(--t-field)); outline: none;
      transition: border-color .15s, box-shadow .15s;
    }
    .field:focus { border-color: rgb(var(--ac)); }
    .field::placeholder { color: rgb(var(--t-ph)); }
    .btn {
      display: inline-flex; align-items: center; justify-content: center; gap: 0.4rem;
      font-size: 0.8125rem; font-weight: 500; padding: 0.5rem 0.9rem; border-radius: 0.625rem;
      background: rgb(var(--raised)); color: rgb(var(--t-btn)); border: 1px solid rgb(var(--line));
      transition: background-color .15s, border-color .15s, color .15s, transform .1s; cursor: pointer;
    }
    .btn:hover { background: rgb(var(--raised2)); border-color: rgb(var(--line-hover)); }
    .btn:active { transform: scale(.97); }
    .btn:focus-visible, .nav-item:focus-visible { outline: 2px solid rgb(var(--ac)); outline-offset: 2px; }
    .btn-primary { background: rgb(var(--ac-strong)); border-color: rgb(var(--ac-strong)); color: rgb(var(--ac-ink)); font-weight: 600; }
    .btn-primary:hover { background: rgb(var(--ac)); border-color: rgb(var(--ac)); }
    #btn-run-cycle:hover { border-color: rgb(var(--ac)); color: rgb(var(--ac-soft)); }
    .nav-marker { box-shadow: inset 2px 0 0 0 rgb(var(--ac)); }
    .btn-danger { background: rgb(var(--danger-bg)); border-color: rgb(var(--danger-bd)); color: rgb(var(--danger-fg)); }
    .btn-danger:hover { background: rgb(var(--danger-bg-h)); border-color: rgb(var(--danger-bd-h)); }
    .btn-sm { padding: 0.35rem 0.7rem; font-size: 0.75rem; }
    .card { background: rgb(var(--surface)); border: 1px solid rgb(var(--line)); border-radius: 0.875rem; }
    .page-title { font-size: 1.125rem; font-weight: 600; color: rgb(var(--t-title)); letter-spacing: -0.01em; }
    .page-sub { font-size: 0.8125rem; color: rgb(var(--t-hint)); margin-top: 0.2rem; }
    .section-title { font-size: 0.8125rem; font-weight: 600; color: rgb(var(--t-label)); letter-spacing: 0.02em; }
    .seg { display: inline-flex; background: rgb(var(--field)); border: 1px solid rgb(var(--line)); border-radius: 0.625rem; padding: 2px; }
    .seg > button { padding: 0.3rem 0.8rem; font-size: 0.8125rem; border-radius: 0.5rem; color: rgb(var(--t-seg)); transition: background-color .15s, color .15s; }
    .seg > button:hover { color: rgb(var(--t-btn)); }

    /* Show cards: the pause/delete buttons fade in on hover. */
    .show-grid { display: grid; gap: 1.25rem; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); }
    @media (min-width: 1024px) { .show-grid { grid-template-columns: repeat(auto-fill, minmax(215px, 1fr)); } }
    /* The poster zoom is a transform, so the browser only scales an existing layer instead
       of laying the image out and resampling it on every frame. The layer bleeds 1px past
       the card so the clipped edge of the scaled layer never lands on a sub-pixel (card
       heights are fractional) and shows as a seam. */
    .poster-zoom { inset: -1px !important; transition: transform .3s cubic-bezier(.2, .8, .2, 1); backface-visibility: hidden; }
    .group:hover .poster-zoom { transform: scale(1.04); }
    .card-actions { opacity: 0; pointer-events: none; transition: opacity .2s ease-out; }
    .group:hover .card-actions, .group:focus-within .card-actions { opacity: 1; pointer-events: auto; }

    #tab-show { background-color: rgb(var(--bg)); }
    body.show-open #sidebar .btn:hover { border-color: rgb(var(--ac) / .7); color: rgb(var(--ac-soft)); }
    #tab-show .card { background-color: rgb(var(--surface) / .92); border-color: rgb(var(--line)); }
    #tab-show .field, #tab-show .seg { background-color: rgb(var(--field)); border-color: rgb(var(--line)); }
    #tab-show .field:focus { border-color: rgb(var(--ac)); box-shadow: 0 0 0 3px rgb(var(--ac) / .2); }
    #tab-show .bg-canvas { background-color: rgb(var(--field) / .85); }
    #tab-show .border-line { border-color: rgb(var(--line)); }
    #tab-show .divide-line-soft > :not([hidden]) ~ :not([hidden]) { border-color: rgb(var(--line-soft)); }
        #tab-show .btn:not(.btn-primary):not(.btn-danger):hover { border-color: rgb(var(--ac) / .7); color: rgb(var(--ac-soft)); }
    #tab-show .section-title { color: rgb(var(--ac-soft)); }
    #tab-show #show-save-bar, #settings-save-bar { border-color: rgb(var(--ac) / .5); background-color: rgb(var(--surface) / .95); }
    #show-backdrop-art, #show-banner-art { contain: paint; will-change: transform; }
    #show-backdrop-art { -webkit-mask-image: linear-gradient(to bottom, #000 0, rgba(0, 0, 0, .5) 45%, transparent 90%); mask-image: linear-gradient(to bottom, #000 0, rgba(0, 0, 0, .5) 45%, transparent 90%); }
    #show-banner-art { -webkit-mask-image: linear-gradient(to bottom, #000 60%, transparent 100%); mask-image: linear-gradient(to bottom, #000 60%, transparent 100%); }
    #tab-show ::selection { background: rgb(var(--ac) / .35); color: #fff; }
    #tab-show * { scrollbar-color: rgb(var(--ac) / .45) transparent; }
    #tab-show ::-webkit-scrollbar-thumb { background: rgb(var(--ac) / .45); }
    @keyframes vt-rise { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: none; } }
    @keyframes vt-drop { from { opacity: 1; transform: none; } to { opacity: 0; transform: translateY(8px); } }
    @keyframes vt-fade-in { from { opacity: 0; } to { opacity: 1; } }
    @keyframes vt-fade-out { from { opacity: 1; } to { opacity: 0; } }
    .page-enter { animation: vt-rise .26s cubic-bezier(.2, .8, .2, 1); }
    @keyframes tab-rise { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }
    .tab-enter { animation: tab-rise .16s cubic-bezier(.2, .8, .2, 1); }
    #main-scroll-container, #tab-show { view-transition-name: content; }
    #toast-container { view-transition-name: toasts; }
    ::view-transition-group(toasts) { animation: none; }
    ::view-transition-old(toasts) { display: none; }
    ::view-transition-new(toasts) { animation: none; }
    html.vt-open::view-transition-old(content) { animation: vt-fade-out .28s cubic-bezier(.2, .8, .2, 1) both; }
    html.vt-open::view-transition-new(content) { animation: vt-rise .28s cubic-bezier(.2, .8, .2, 1) both; }
    html.vt-close::view-transition-old(content) { animation: vt-drop .28s cubic-bezier(.2, .8, .2, 1) both; }
    html.vt-close::view-transition-new(content) { animation: vt-fade-in .28s cubic-bezier(.2, .8, .2, 1) both; }
    ::view-transition-group(show-poster) { animation-duration: .32s; animation-timing-function: cubic-bezier(.2, .8, .2, 1); }
    /* Keep the transition cheap to composite: the page background, sidebar and top bar do
       not change, so they are no longer separate snapshots (two fewer captures per
       transition) and the root is shown as it is instead of cross-fading two full copies; the page content blends normally rather than additively; and the poster keeps
       its aspect while it morphs instead of being stretched and resampled. */
    ::view-transition-group(root), ::view-transition-old(root), ::view-transition-new(root) { animation: none; }
    ::view-transition-old(root) { display: none; }
    ::view-transition-old(content), ::view-transition-new(content) { mix-blend-mode: normal; }
    ::view-transition-old(show-poster), ::view-transition-new(show-poster) { height: 100%; object-fit: cover; }
    /* The blurred art is drawn small and scaled up: a blur costs by area, so this gives
       the same soft look for about 1/64 of the work. */
    .blur-art { position: absolute; left: 0; top: 0; width: 12.5%; height: 12.5%; object-fit: cover; transform-origin: 0 0; }
    .blur-art-backdrop { transform: scale(8.8); filter: blur(5px) saturate(1.5); opacity: .6; }
    .blur-art-banner { transform: scale(10); filter: blur(5px); opacity: .6; }

    @media (prefers-reduced-motion: reduce) {
      ::view-transition-group(*), ::view-transition-old(*), ::view-transition-new(*) { animation: none !important; }
      .page-enter, .tab-enter { animation: none; }
      .group:hover .poster-zoom { transform: none; }
    }

    @media (hover: none) {
      .card-actions { opacity: 1 !important; pointer-events: auto !important; }
    }
  </style>
  <script>
    try {
      var cachedAccent = localStorage.getItem('kisetsu_accent_css2');
      if (cachedAccent) {
        var accentStyle = document.createElement('style');
        accentStyle.id = 'user-accent';
        accentStyle.textContent = cachedAccent;
        document.head.appendChild(accentStyle);
      }
    } catch (err) {}
  </script>
</head>
<body class="bg-canvas text-zinc-100 flex h-dvh overflow-hidden font-sans antialiased selection:bg-accent/30 selection:text-zinc-50">

  <div id="sidebar-backdrop" onclick="toggleSidebar(false)" class="fixed inset-0 bg-black/60 z-30 hidden md:hidden"></div>

  <aside id="sidebar" class="fixed md:static inset-y-0 left-0 w-52 -translate-x-full md:translate-x-0 transition-transform duration-200 bg-chrome flex flex-col flex-shrink-0 select-none z-40">

    <div class="px-4 pt-4 pb-2 flex items-center justify-between">
      <span class="text-sm font-semibold text-zinc-100">Kisetsu</span>
      <button type="button" id="btn-theme" onclick="toggleTheme()" title="Switch to light theme" aria-label="Switch to light theme" class="w-7 h-7 -mr-1.5 rounded-md flex items-center justify-center text-zinc-400 hover:text-zinc-100 hover:bg-raised transition-colors">
        <svg id="icon-theme-sun" class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="1.8"><circle cx="12" cy="12" r="4"/><path stroke-linecap="round" d="M12 3v1.5M12 19.5V21M3 12h1.5M19.5 12H21M5.6 5.6l1.1 1.1M17.3 17.3l1.1 1.1M5.6 18.4l1.1-1.1M17.3 6.7l1.1-1.1"/></svg>
        <svg id="icon-theme-moon" class="w-4 h-4 hidden" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="1.8"><path stroke-linecap="round" stroke-linejoin="round" d="M20.5 14.5A8.5 8.5 0 019.5 3.5a8.5 8.5 0 1011 11z"/></svg>
      </button>
    </div>

    <nav class="flex-1 px-2 py-1 space-y-0.5 overflow-y-auto">
      <button onclick="switchTab('shows')" id="nav-shows" class="nav-item w-full flex items-center gap-2.5 px-2.5 py-1.5 rounded-md text-sm font-medium transition-colors bg-raised2 text-zinc-50 nav-marker [&>svg]:text-accent">
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

      <div class="flex gap-2">
        <button onclick="runCycleNow()" id="btn-run-cycle" title="Re-sync AniList, refresh RSS feeds, grab or confirm new episodes and reconcile with qBittorrent" class="btn btn-sm flex-1">
          <svg id="spinner-run-cycle" class="w-4 h-4 hidden animate-spin text-zinc-400" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z"></path></svg>
          <span id="text-run-cycle">Sync Now</span>
        </button>

        <button onclick="togglePause()" id="btn-pause" title="Pause downloading" aria-label="Pause downloading" class="btn btn-sm px-2.5 flex-shrink-0">
          <svg id="icon-pause" class="w-4 h-4" fill="currentColor" viewBox="0 0 24 24"><path d="M7 5h3.5v14H7zM13.5 5H17v14h-3.5z"/></svg>
          <svg id="icon-resume" class="w-4 h-4 hidden text-amber-400" fill="currentColor" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
        </button>
      </div>
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
        <span id="stat-all-paused" class="text-amber-400 hidden">Paused</span>
        <span id="stat-working" class="text-emerald-400">0 Working</span>
        <span id="stat-testing" class="text-amber-400 hidden">0 Testing</span>
        <span id="stat-upcoming" class="text-sky-400">0 Upcoming</span>
        <span id="stat-stalled" class="text-rose-400 hidden">0 Stalled</span>
      </div>
    </header>

    <div class="flex-1 overflow-y-auto px-4 pt-5 pb-6 md:px-8 md:pt-5 md:pb-8" id="main-scroll-container">

      <section id="tab-shows" class="space-y-5 max-w-[1900px]">

        <div id="section-releasing" class="space-y-4">
          <div class="flex items-center justify-between gap-3">
            <div class="flex items-baseline gap-2">
              <h2 class="section-title">Releasing</h2>
              <span id="header-count-releasing" class="text-[11px] font-medium tabular-nums px-1.5 py-px rounded-full bg-accent/15 text-accent-soft">0</span>
            </div>
            <div class="flex items-center gap-2">
            <button type="button" onclick="autoDiscoverShows()" class="btn btn-sm" title="Reset every show to auto-discover. Hand-picked feeds are cleared; shows locked to a feed they downloaded from are kept.">Auto-discover</button>
            <div class="flex items-center gap-0.5 bg-sunken p-0.5 rounded-lg border border-line-soft text-xs">
              <button onclick="setSortMode('airing')" id="sort-btn-airing" class="px-2.5 py-1 rounded-md flex items-center gap-1.5 transition-colors text-zinc-400 hover:text-zinc-200" title="Soonest airing first">
                <svg class="w-3.5 h-3.5 text-zinc-400" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
                <span>Airing</span>
              </button>
              <button onclick="setSortMode('title')" id="sort-btn-title" class="px-2.5 py-1 rounded-md flex items-center gap-1.5 transition-colors text-zinc-400 hover:text-zinc-200" title="A to Z">
                <svg class="w-3.5 h-3.5 text-zinc-400" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M3 4h13M3 8h9m-9 4h6m4 0l4-4m0 0l4 4m-4-4v12"/></svg>
                <span>A-Z</span>
              </button>
              <button onclick="setSortMode('default')" id="sort-btn-default" class="px-2.5 py-1 rounded-md flex items-center gap-1.5 transition-colors text-zinc-400 hover:text-zinc-200" title="Order added">
                <svg class="w-3.5 h-3.5 text-zinc-400" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M4 6h16M4 10h16M4 14h16M4 18h16"/></svg>
                <span>Default</span>
              </button>
            </div>
            </div>
          </div>
          <div id="grid-releasing" class="show-grid">
          </div>
        </div>

        <div id="section-completed" class="space-y-4">
          <div class="flex items-center justify-between gap-3">
            <div class="flex items-baseline gap-2">
              <h2 class="section-title">Completed</h2>
              <span id="header-count-completed" class="text-[11px] font-medium tabular-nums px-1.5 py-px rounded-full bg-violet-500/15 text-violet-400">0</span>
            </div>
          </div>
          <div id="grid-completed" class="show-grid">
          </div>
        </div>

        <div id="section-planned" class="space-y-4">
          <div class="flex items-center justify-between gap-3">
            <div class="flex items-baseline gap-2">
              <h2 class="section-title">Planned</h2>
              <span id="header-count-planned" class="text-[11px] font-medium tabular-nums px-1.5 py-px rounded-full bg-sky-500/15 text-sky-400">0</span>
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
          <p class="page-sub !mt-0">Checked in this order. Drag to reorder.</p>
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
                  <th class="py-3 px-2 w-20 text-center">Priority</th>
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

        <form id="settings-form" onsubmit="saveSettings(event)" oninput="updateSettingsSaveBar()" onchange="updateSettingsSaveBar()" class="space-y-5">

          <div>
            <h3 class="section-title mb-2 px-1 flex items-center gap-2"><svg class="w-4 h-4 text-accent" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="1.8"><path stroke-linecap="round" stroke-linejoin="round" d="M5 12h14M5 12a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v4a2 2 0 01-2 2M5 12a2 2 0 00-2 2v4a2 2 0 002 2h14a2 2 0 002-2v-4a2 2 0 00-2-2m-2-4h.01M17 16h.01"/></svg>qBittorrent</h3>
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
            <h3 class="section-title mb-2 px-1 flex items-center gap-2"><svg class="w-4 h-4 text-accent" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="1.8"><path stroke-linecap="round" stroke-linejoin="round" d="M4 16v2a2 2 0 002 2h12a2 2 0 002-2v-2M8 12l4 4m0 0l4-4m-4 4V4"/></svg>Downloads</h3>
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
            <h3 class="section-title mb-2 px-1 flex items-center gap-2"><svg class="w-4 h-4 text-accent" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="1.8"><path stroke-linecap="round" stroke-linejoin="round" d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z"/></svg>AniList &amp; schedule</h3>
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
            <h3 class="section-title mb-2 px-1 flex items-center gap-2"><svg class="w-4 h-4 text-accent" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="1.8"><path stroke-linecap="round" stroke-linejoin="round" d="M7 21a4 4 0 01-4-4V5a2 2 0 012-2h4a2 2 0 012 2v12a4 4 0 01-4 4zm0 0h12a2 2 0 002-2v-4a2 2 0 00-2-2h-2.343M11 7.343l1.657-1.657a2 2 0 012.828 0l2.829 2.829a2 2 0 010 2.828l-8.486 8.485M7 17h.01"/></svg>Appearance &amp; behaviour</h3>
            <div class="card p-4 grid grid-cols-1 sm:grid-cols-2 gap-x-4 gap-y-4">
              <div>
                <div class="field-label" title="Saved in this browser only. System follows your OS setting.">Theme</div>
                <div class="seg">
                  <button type="button" onclick="setThemeMode('system')" id="btn-theme-system">System</button>
                  <button type="button" onclick="setThemeMode('light')" id="btn-theme-light">Light</button>
                  <button type="button" onclick="setThemeMode('dark')" id="btn-theme-dark">Dark</button>
                </div>
              </div>
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

          <div class="sticky bottom-5 h-0 z-30">
            <div id="settings-save-bar" inert aria-hidden="true" class="absolute bottom-0 left-1/2 -translate-x-1/2 card shadow-xl shadow-black/50 pl-4 pr-2.5 py-2.5 flex items-center gap-4 opacity-0 translate-y-3 pointer-events-none transition-[transform,opacity] duration-200 ease-out motion-reduce:transition-none">
              <span class="text-sm text-zinc-300 whitespace-nowrap">Unsaved changes</span>
              <div class="flex items-center gap-2">
                <button type="button" onclick="discardSettingsChanges()" class="btn btn-sm">Discard</button>
                <button type="submit" id="btn-save-settings" class="btn btn-sm btn-primary">Save changes</button>
              </div>
            </div>
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
          <p class="page-sub !mt-0">What Kisetsu has been doing.</p>

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
        <button type="button" onclick="closeShowPage()" class="absolute top-3 left-4 md:top-4 md:left-8 w-8 h-8 rounded-md flex items-center justify-center on-art bg-black/50 hover:bg-black/70 backdrop-blur text-zinc-100 transition-colors active:scale-90" aria-label="Back" title="Back (Esc)">
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
      'FIXED': { label: 'Working', bg: 'st-ok' },
      'UNCONFIRMED': { label: 'Testing', bg: 'st-warn' },
      'UPCOMING': { label: 'Upcoming', bg: 'st-info' },
      'STALLED': { label: 'Stalled', bg: 'st-bad' },
      'COMPLETED': { label: 'Completed', bg: 'st-done' },
      'PAUSED': { label: 'Paused', bg: 'st-idle' },
    };

    const NAV_INACTIVE_CLASS = 'nav-item w-full flex items-center gap-2.5 px-2.5 py-1.5 rounded-md text-sm font-medium transition-colors text-zinc-400 hover:text-zinc-100 hover:bg-raised';
    const NAV_ACTIVE_CLASS = 'nav-item w-full flex items-center gap-2.5 px-2.5 py-1.5 rounded-md text-sm font-medium transition-colors bg-raised2 text-zinc-50 nav-marker [&>svg]:text-accent';
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

    function renderShows() {
      updateSortButtonStyles();
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

      setGridHtml(gridReleasing, releasingShows.map(renderShowCard).join('') || '<div class="col-span-full py-6 text-center text-zinc-500 text-sm">No currently releasing anime.</div>');
      setGridHtml(gridPlanned, plannedShows.map(renderShowCard).join('') || '<div class="col-span-full py-6 text-center text-zinc-500 text-sm">No planned upcoming anime.</div>');
      setGridHtml(gridCompleted, completedShows.map(renderShowCard).join('') || '<div class="col-span-full py-6 text-center text-zinc-500 text-sm">No completed anime.</div>');

      // Releasing is always shown so the page is never blank; the trailing
      // sections are noise when they have nothing in them.
      document.getElementById('section-completed').classList.toggle('hidden', completedShows.length === 0);
      document.getElementById('section-planned').classList.toggle('hidden', plannedShows.length === 0);
      fitShowGrids();
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

    function renderShowCard(show) {
      const { isPaused, isCompleted, statusKey, cfg, label } = resolveShowStatus(show);

      // Overlay line: "Ep n" on the left, countdown or outcome on the right.
      const downloaded = show.downloaded_episodes_count || 0;
      const total = show.total_episodes || 0;
      const { epLabel, airInfo, airClass, countdownAttr } = showAirSummary(show, statusKey);

      // The count is shown whenever something is downloaded. The bar also covers a show
      // whose total is unknown, filling against the episodes that are out (progressFraction).
      const showProgress = downloaded > 0 || total > 0;
      const showBar = total > 0 || downloaded > 0 || !!show.next_airing_episode;
      const pct = showBar ? Math.round(progressFraction(show) * 100) : 0;
      const barClass = (isPaused || isCompleted) ? 'bg-zinc-300' : 'bg-[rgb(var(--ac-art))]';
      const progressText = `${downloaded}/${total || '?'}`;
      const hasOverlay = epLabel || airInfo || showProgress || showBar;

      const name = escapeHtml(show.display_name);
      const isDimmed = isPaused || isCompleted;
      const dimClass = isDimmed ? 'opacity-80 grayscale-[35%]' : '';
      const noArt = `<div class="absolute inset-0 flex items-center justify-center bg-zinc-900 text-zinc-600 text-xs ${dimClass}">No Art</div>`;
      const art = show.cover_image
        ? `${noArt}<img src="${escapeHtml(show.cover_image)}" alt="${name}" class="absolute inset-0 w-full h-full object-cover ${dimClass}" loading="lazy" onerror="this.onerror=null;this.style.display='none'">`
        : noArt;
      // The shade lives inside the zooming layer, so the art and its shade scale as one
      // unit and no unshaded strip of poster can show behind the episode text.
      const shade = hasOverlay ? '<div class="absolute inset-x-0 bottom-0 h-2/5 bg-gradient-to-t from-black/90 via-black/55 to-transparent"></div>' : '';
      const posterImg = `<div class="absolute inset-0 overflow-hidden"><div class="poster-zoom absolute inset-0">${art}${shade}</div></div>`;

      // A tag only says what the section and the countdown don't already say, so Working
      // (the normal state) gets none, nor do Upcoming in Planned or Completed in Completed.
      const tagTips = {
        Testing: 'No release seen yet. Trying the feeds by priority.',
        Stalled: 'The download has stopped making progress.',
        Paused: 'Monitoring is paused for this show.',
      };
      const statusChip = tagTips[label]
        ? `<span class="px-1.5 py-0.5 inline-flex items-center rounded text-[11px] font-medium border ${cfg.bg}" title="${tagTips[label]}">${label}</span>`
        : '';

      const overlay = hasOverlay ? `
            <div class="absolute inset-x-0 bottom-0 z-10 pointer-events-none px-2.5 pb-2.5">
              <div class="flex items-baseline justify-between gap-2 text-[13px]">
                <span class="font-semibold text-white">${epLabel}</span>
                <span class="show-countdown font-medium tabular-nums ${airClass}" ${countdownAttr} title="${localDateTime(show.next_airing_at)}">${airInfo}</span>
              </div>
              ${showProgress ? `<div class="mt-0.5 text-right text-[10px] tabular-nums text-zinc-400">${progressText}</div>` : ''}
            </div>
            ${showBar ? `<div class="absolute inset-x-0 bottom-0 h-[5px] z-10 bg-white/25"><div class="h-full ${barClass}" style="width:${pct}%"></div></div>` : ''}` : '';

      const pauseIcon = isPaused
        ? `<svg class="w-3.5 h-3.5 ml-0.5" fill="currentColor" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>`
        : `<svg class="w-3.5 h-3.5" fill="currentColor" viewBox="0 0 24 24"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>`;

      const feedName = show.current_feed_name ? escapeHtml(show.current_feed_name) : 'No feed';
      const lockIcon = show.feed_learned
        ? `<svg class="w-3 h-3 flex-shrink-0 text-zinc-600" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2"><title>Feed locked</title><path stroke-linecap="round" stroke-linejoin="round" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z"/></svg>`
        : '';

      return `
        <div onclick="openShowPage(${show.id})" onpointerenter="prefetchShowPage(${show.id})" class="bg-surface border ${isDimmed ? 'border-line-soft' : 'border-line'} hover:border-line-hover rounded-lg show-card flex flex-col overflow-hidden group cursor-pointer transition-colors duration-150 ease-out">

          <div data-poster-id="${show.id}" class="on-art relative w-full aspect-[2/3] bg-zinc-900 overflow-hidden">
            ${posterImg}
            ${overlay}

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

          <div class="px-3 pt-2.5 pb-3 flex flex-col gap-1">
            <h3 class="text-sm font-medium text-zinc-200 group-hover:text-zinc-50 leading-[1.4] line-clamp-2 min-h-[2.55rem] overflow-hidden pb-[1px]" title="${name}">${name}</h3>
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

      // The dot is the show's state; it fades once the slot has aired.
      const stateOf = (i) => (i.show.status || '').toUpperCase();
      const live = slot.items.filter(i => !i.isPaused);
      const has = (key) => live.some(i => stateOf(i) === key);
      let dotColor = 'bg-sky-400';
      if (!live.length) dotColor = 'bg-zinc-600';
      else if (has('STALLED')) dotColor = 'bg-rose-400';
      else if (has('UNCONFIRMED') && live.some(i => i.show.is_released)) dotColor = 'bg-amber-400';
      else if (has('FIXED')) dotColor = 'bg-emerald-400';
      if (slot.hasPassed && live.length) dotColor += '/60';

      if (!isMulti) {
        const item = slot.items[0];
        const show = item.show;
        const statusKey = (show.status || '').toUpperCase();
        const isPaused = item.isPaused;
        const hasPassed = item.hasPassed;

        const titleColor = ((isPaused || hasPassed) ? 'text-zinc-400' : 'text-zinc-100') + ' group-hover:text-accent-soft transition-colors';

        const posterImg = show.cover_image
          ? `<img src="${escapeHtml(show.cover_image)}" alt="${escapeHtml(show.display_name)}" class="w-16 aspect-[2/3] rounded-md object-cover flex-shrink-0 bg-canvas ${isPaused ? 'grayscale' : ''}" loading="lazy" onerror="this.onerror=null;this.style.display='none'">`
          : `<div class="w-16 aspect-[2/3] bg-canvas border border-zinc-800 rounded-md flex items-center justify-center text-[10px] text-zinc-500 flex-shrink-0">No Art</div>`;

        let epText = show.next_airing_episode ? `Ep ${show.next_airing_episode}` : (show.last_confirmed_episode ? `Ep ${show.last_confirmed_episode}` : 'Ep 1');
        const anilistUrl = show.anilist_id ? `https://anilist.co/anime/${show.anilist_id}` : '#';

        return `
          <div class="relative group select-none">
            <div class="flex items-center justify-between gap-1.5 mb-1.5">
              <div class="flex items-center gap-1.5">
                <span class="w-2 h-2 rounded-full ${dotColor} absolute -left-[14px] top-[6px] z-10"></span>
                <span class="text-[13px] tabular-nums ${isPaused ? 'text-zinc-400' : (hasPassed ? 'text-zinc-400' : 'text-zinc-100')} font-medium">${slot.timeStr}</span>
              </div>
              <span class="text-xs tabular-nums font-medium ${isPaused ? 'text-zinc-500' : (hasPassed ? 'text-zinc-500' : 'text-zinc-400')}">${epText}</span>
            </div>

            <a href="${anilistUrl}" target="_blank" rel="noopener noreferrer" class="flex gap-3 items-start cursor-pointer transition-opacity ${isPaused ? 'grayscale opacity-50 hover:opacity-80' : (hasPassed ? 'opacity-80 hover:opacity-100' : 'opacity-100 hover:opacity-90')}">
              ${posterImg}

              <div class="flex-1 min-w-0 pt-0.5">
                <h4 class="text-sm font-medium leading-snug line-clamp-3 ${titleColor}" title="${escapeHtml(show.display_name)}">
                  ${escapeHtml(show.display_name)}
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

        const titleColor = ((isPaused || hasPassed) ? 'text-zinc-400' : 'text-zinc-100') + ' group-hover:text-accent-soft transition-colors';

        const posterImg = show.cover_image
          ? `<img src="${escapeHtml(show.cover_image)}" alt="${escapeHtml(show.display_name)}" class="w-16 aspect-[2/3] rounded-md object-cover flex-shrink-0 bg-canvas ${isPaused ? 'grayscale' : ''}" loading="lazy" onerror="this.onerror=null;this.style.display='none'">`
          : `<div class="w-16 aspect-[2/3] bg-canvas border border-zinc-800 rounded-md flex items-center justify-center text-[10px] text-zinc-500 flex-shrink-0">No Art</div>`;

        let epText = show.next_airing_episode ? `Ep ${show.next_airing_episode}` : (show.last_confirmed_episode ? `Ep ${show.last_confirmed_episode}` : 'Ep 1');
        const anilistUrl = show.anilist_id ? `https://anilist.co/anime/${show.anilist_id}` : '#';

        return `
          <a href="${anilistUrl}" target="_blank" rel="noopener noreferrer" class="flex gap-3 items-start cursor-pointer transition-opacity ${isPaused ? 'grayscale opacity-50 hover:opacity-80' : (hasPassed ? 'opacity-80 hover:opacity-100' : 'opacity-100 hover:opacity-90')}">
            ${posterImg}

            <div class="flex-1 min-w-0 pt-0.5">
              <div class="flex items-start justify-between gap-1">
                <h4 class="text-sm font-medium leading-snug line-clamp-3 ${titleColor}" title="${escapeHtml(show.display_name)}">
                  ${escapeHtml(show.display_name)}
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
              <span class="w-2 h-2 rounded-full ${dotColor} absolute -left-[14px] top-[6px] z-10"></span>
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
      const groups = (feed && feed.feed_groups) || [];
      groups.filter(g => g.usable).forEach(g => g.matches.forEach(m => {
        const best = replacements.get(m.episode);
        if (m.replaces && (!best || m.version > best.version)) replacements.set(m.episode, m);
      }));

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
        const groups = feed ? (feed.feed_groups || []) : [];
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
          </li>`;
        const feedRows = groups.map(g => groupHeader(g) + g.matches.map(m => matchRow(g, m)).join('')).join('');
        const emptyFeedText = 'Nothing in any feed matches this show right now. Try Search older.';
        return `
          <div class="flex-1 min-h-0 flex flex-col gap-3">
            <div class="flex items-center justify-between gap-3">
              <div class="seg">
                <button type="button" id="show-tab-episodes" onclick="setShowListTab('episodes')" class="${showListTab === 'episodes' ? SEG_ACTIVE_CLASS : SEG_INACTIVE_CLASS}">Episodes</button>
                <button type="button" id="show-tab-feed" onclick="setShowListTab('feed')" class="${showListTab === 'feed' ? SEG_ACTIVE_CLASS : SEG_INACTIVE_CLASS}">Releases</button>
              </div>
              <span class="flex items-center gap-3">
                <span class="text-xs text-zinc-500 tabular-nums">${episodes.length} tracked · ${feed ? `${matches.length} release${matches.length === 1 ? '' : 's'}` : 'checking feed…'}</span>
                <button type="button" onclick="searchPastReleases(this)" class="text-xs text-zinc-400 hover:text-zinc-100 underline underline-offset-4 decoration-accent/50 hover:decoration-accent transition-colors disabled:opacity-50" title="Ask the feeds' sites for this show's older releases, for episodes that are no longer in their RSS">Search older</button>
              </span>
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
        <h3 class="section-title mb-2 px-1">Episodes</h3>
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
          if (cd === 'Aired') airClass = 'text-amber-300';
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
            <span class="truncate">${feedName}</span>${lockIcon}
          </span>
        </div>
        ${showProgress ? `
        <div class="flex items-center gap-3 max-w-sm mt-3">
          <div class="flex-1 h-2 rounded-full bg-zinc-100/15 overflow-hidden"><div class="h-full ${barClass}" style="width:${pct}%"></div></div>
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
    // `mode` is 'dark' or 'light'. Light keeps the tint weaker, since saturation reads
    // stronger on pale surfaces, and uses darker accents so accent text stays readable.
    function paletteFromHues([h1, s1, h2, th, ts], mode = 'dark') {
      const light = mode === 'light';
      const s = Math.min(0.85, Math.max(0.4, s1));
      const warm = th >= 10 && th <= 60 ? 0.7 : 1;
      const t = Math.min(0.5, ts) * THEME_TINT * warm * (light ? 0.5 : 1);
      const surface = (l, k = 1) => hslToRgb(th, t * k, l).join(' ');
      const strong = hslToRgb(h1, s, light ? 0.36 : 0.56);
      const surfaces = light ? {
        bg: surface(0.947), sunken: surface(0.905), chrome: surface(0.922), surface: surface(0.982), field: surface(0.992),
        raised: surface(0.935), raised2: surface(0.895), line: surface(0.862, 0.9), lineSoft: surface(0.9, 0.9),
        lineHover: surface(0.8, 0.9), lineStrong: surface(0.68, 0.9),
      } : {
        bg: surface(0.085), sunken: surface(0.065), chrome: surface(0.10), surface: surface(0.115), field: surface(0.07),
        raised: surface(0.155), raised2: surface(0.19), line: surface(0.21, 0.9), lineSoft: surface(0.16, 0.9),
        lineHover: surface(0.26, 0.9), lineStrong: surface(0.32, 0.9),
      };
      return {
        ...surfaces,
        ac: hslToRgb(h1, s, light ? 0.40 : 0.62).join(' '),
        soft: hslToRgb(h1, s, light ? 0.31 : 0.76).join(' '),
        strong: strong.join(' '),
        ink: relativeLuminance(strong) < 0.2 ? '255 255 255' : '8 10 14',
        ac2: hslToRgb(h2, s, light ? 0.40 : 0.62).join(' '),
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
    const ACCENT_CSS_KEY = 'kisetsu_accent_css2';
    const STOCK_ACCENT = {
      dark: { ac: '45 212 191', soft: '94 234 212', strong: '20 184 166', ink: '4 32 30', ac2: '56 189 248' },
      light: { ac: '13 148 136', soft: '15 118 110', strong: '15 118 110', ink: '255 255 255', ac2: '2 132 199' },
    };
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
        if (color !== DEFAULT_ACCENT) {
          const s = Math.min(0.9, Math.max(0.4, sat));
          const lightness = light ? 0.40 : Math.min(0.72, Math.max(0.55, l));
          const strong = hslToRgb(h, s, light ? 0.36 : lightness - 0.08);
          accent = {
            ac: hslToRgb(h, s, lightness).join(' '),
            soft: hslToRgb(h, s, light ? 0.31 : Math.min(0.88, lightness + 0.13)).join(' '),
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
      const ring = (active) => active ? 'ring-2 ring-offset-2 ring-offset-surface ring-zinc-100' : 'hover:ring-2 hover:ring-zinc-100/30 hover:ring-offset-2 hover:ring-offset-surface';
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
        <h3 class="section-title mb-2 px-1 flex items-center gap-2">Feed &amp; matching<span class="ml-auto text-xs font-normal text-zinc-500">${ruleStatusText}</span></h3>
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
         <div class="flex flex-wrap items-center gap-x-5 gap-y-2">
          <button type="button" onclick="showTriggerRediscover()" class="text-sm text-zinc-400 hover:text-zinc-100 underline underline-offset-4 decoration-accent/50 hover:decoration-accent transition-colors" title="Clear manual rule customizations and let the supervisor auto-match against feeds">
           Reset to auto-discover
          </button>
          ${isDirect ? '' : `<button type="button" onclick="searchPastReleases(this)" class="text-sm text-zinc-400 hover:text-zinc-100 underline underline-offset-4 decoration-accent/50 hover:decoration-accent transition-colors disabled:opacity-50" title="Ask the feeds' sites for this show's older releases, for episodes that are no longer in their RSS">
           Search past releases
          </button>`}
         </div>
        </div>`;

      document.getElementById('show-col-feed').innerHTML = `
        <h3 class="section-title mb-2 px-1">Download</h3>
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
        ? `\n\n${unfinishedElsewhere} unfinished download(s) from the old feed will be canceled and fetched again from the new one.`
        : '';
      if (feedChanged && inspectedShow && inspectedShow.feed_learned && now.current_feed_id !== inspectedShow.learned_feed_id) {
        const lockedName = inspectedShow.learned_feed_name || 'the feed that delivered';
        const target = allFeeds.find(f => f.id === now.current_feed_id);
        const targetName = target ? `'${target.qbit_feed_name}'` : 'auto-detect';
        const ok = confirm(
          `'${inspectedShow.display_name}' is locked to '${lockedName}' because a release was recorded from it.\n\n` +
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

    // Moves the show to a feed that carries its releases. The save does the rest:
    // the lock confirmation, the canceled-downloads note and the pin.
    function useFeedFromReleases(feedId) {
      const select = document.getElementById('show-feed-id');
      if (!select) return;
      select.value = String(feedId);
      return saveShowPage();
    }

    function searchPastReleases(btn) {
      const showId = currentInspectedShowId;
      if (!showId) return;
      return once(`search-releases-${showId}`, async () => {
        btn.disabled = true;
        try {
          const data = await apiFetch(`/api/shows/${showId}/search-releases`, { method: 'POST' });
          const episodes = (data.missing || []).join(', ');
          showToast(
            !data.searched ? 'Every aired episode is already in the feeds or downloaded.'
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
          btn.disabled = false;
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
      if (!confirm(`Reset every show to auto-discover?\n\nHand-picked feeds are cleared. Shows locked to a feed they already downloaded from, and completed shows, are kept.`)) return;
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
        await apiFetch('/api/feeds/reorder', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(reorderPayload)
        });
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
          <td class="py-4 px-2 text-accent text-sm font-medium tabular-nums">
            <div class="flex items-center justify-center gap-1.5">
              <span>#${f.priority}</span>
              <span class="flex flex-col text-zinc-500 opacity-0 group-hover:opacity-100 focus-within:opacity-100 transition-opacity">
                <button onclick="event.stopPropagation(); moveFeed(${idx}, ${idx - 1})" class="px-1 hover:text-zinc-50 disabled:opacity-20" aria-label="Move up" title="Move up" ${idx === 0 ? 'disabled' : ''}><svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M5 15l7-7 7 7"/></svg></button>
                <button onclick="event.stopPropagation(); moveFeed(${idx}, ${idx + 1})" class="px-1 hover:text-zinc-50 disabled:opacity-20" aria-label="Move down" title="Move down" ${idx === allFeeds.length - 1 ? 'disabled' : ''}><svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M19 9l-7 7-7-7"/></svg></button>
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
            <div class="text-[11px] text-zinc-600 mt-0.5 truncate max-w-[12rem]"><span class="${added ? 'text-accent' : 'text-sky-400'}">${verb}</span>${h.feed_name ? ` · ${escapeHtml(h.feed_name)}` : ''}</div>
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
          <span class="text-accent/70 select-none text-[11px] font-mono shrink-0">${timeStr}</span>
          <span class="text-zinc-200 break-all select-text font-mono text-[11px] flex-1">${mark}${escapeHtml(l.message || '')}</span>
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
        const workingEl = document.getElementById('stat-working');
        workingEl.textContent = `${st.counts.works} Working`;
        const testingEl = document.getElementById('stat-testing');
        testingEl.textContent = `${st.counts.testing} Testing`;
        testingEl.classList.toggle('hidden', !st.counts.testing);
        const upcomingEl = document.getElementById('stat-upcoming');
        upcomingEl.textContent = `${st.counts.upcoming} Upcoming`;
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

    applyTheme();
    new ResizeObserver(fitShowGrids).observe(document.getElementById('main-scroll-container'));
    window.addEventListener('resize', fitShowGrids);
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
