import { html, useEffect, useState } from '../ui.js';
import { useResource } from '../store.js';
import { feeds as feedsData } from '../data.js';
import { Icon } from '../components/Icon.js';
import { FeedItems } from '../components/FeedItems.js';
import { FeedsTable, importFromQbit, openFeedDialog } from '../components/FeedsTable.js';

const KEY = 'kisetsu.feeds.releases';
const saved = () => { try { return localStorage.getItem(KEY) === '1'; } catch (err) { return false; } };
const save = (on) => { try { localStorage.setItem(KEY, on ? '1' : '0'); } catch (err) { /* only a convenience */ } };

export function Feeds() {
  const { data } = useResource(feedsData);
  const [picked, setPicked] = useState(null);
  const [open, setOpen] = useState(saved);
  // The panel is mounted a frame before it is shown and stays mounted until it has faded out, so both directions animate.
  const [mounted, setMounted] = useState(open);
  const [visible, setVisible] = useState(open);
  const selected = data && data.some(feed => feed.id === picked) ? picked : null;
  useEffect(() => {
    if (open) {
      setMounted(true);
      const frame = requestAnimationFrame(() => requestAnimationFrame(() => setVisible(true)));
      return () => cancelAnimationFrame(frame);
    }
    setVisible(false);
    const timer = setTimeout(() => setMounted(false), 400);
    return () => clearTimeout(timer);
  }, [open]);
  const show = (on) => { setOpen(on); save(on); };
  // Clicking a feed while the releases are hidden opens them on that feed.
  const pick = (id) => {
    if (open) { setPicked(id); return; }
    if (id !== null) setPicked(id);
    show(true);
  };
  return html`<div class=${`page full feeds-page${mounted ? ' with-side' : ''}${visible ? ' open' : ''}`}>
    <div class="feeds-layout">
      <div class="feeds-col">
        <div class="feeds-tools">
          <button type="button" class="btn sm" onClick=${() => openFeedDialog()}>Add feed</button>
          <button type="button" class="btn sm" onClick=${importFromQbit} title="Add the feeds qBittorrent has that Kisetsu does not">Import from qBittorrent</button>
          <button type="button" class="btn sm feeds-toggle" aria-expanded=${String(visible)} onClick=${() => show(!open)} title=${visible ? 'Hide the releases the feeds carry' : 'Show the releases the feeds carry'}>
            <span>Releases</span><${Icon} name="down" small class="chev" />
          </button>
        </div>
        <p class="page-sub feeds-hint">When several feeds carry a release, the higher one wins. Drag to reorder.</p>
        <div class="card feeds-card">${data ? html`<${FeedsTable} feeds=${data} selected=${visible ? selected : null} onSelect=${pick} />` : null}</div>
      </div>
      ${data && mounted ? html`<div class="feeds-side"><${FeedItems} feeds=${data} selected=${selected} onSelect=${pick} /></div>` : null}
    </div>
  </div>`;
}
