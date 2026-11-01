import { html, useMemo, useState } from '../ui.js';
import { useResource } from '../store.js';
import { go, prefetch, useNow } from '../router.js';
import { shows as showsData } from '../data.js';
import { Icon } from '../components/Icon.js';
import { Poster, StatusTag } from '../components/ShowSummary.js';

const WEEK = 7 * 86400000;
const time = (date) => date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });

const weeksAfter = (date, weeks) => { const at = new Date(date); at.setDate(at.getDate() + 7 * weeks); return at; };

// A show's episodes airing between `from` and `to`. Known air dates are used as they are, so a break week stays empty.
// Past the last known date (or with none at all) a still-airing show is assumed weekly, up to its last episode.
function airingsBetween(show, from, to) {
  const total = show.progress && show.progress.total;
  const known = (show.airings || []).map(a => ({ episode: a.episode, at: new Date(a.at), done: a.done })).filter(a => !isNaN(a.at));
  const found = known.filter(a => a.at >= from && a.at <= to);
  if (!show.next_airing) return found;
  let anchor = known[known.length - 1];
  if (!anchor) {
    const at = new Date(show.next_airing.at);
    if (isNaN(at)) return found;
    anchor = { episode: show.next_airing.episode || (show.last_done || 0) + 1, at };
    // With no schedule, weeks before the next episode are guesses too.
    for (let k = Math.ceil((from - at) / WEEK) - 1; k < 0; k++) {
      const guess = { episode: anchor.episode + k, at: weeksAfter(at, k) };
      if (guess.episode >= 1 && guess.at >= from && guess.at <= to) found.push(guess);
    }
    if (anchor.at >= from && anchor.at <= to) found.push(anchor);
  }
  for (let k = Math.max(1, Math.floor((from - anchor.at) / WEEK)); ; k++) {
    const guess = { episode: anchor.episode + k, at: weeksAfter(anchor.at, k) };
    if (guess.at > to || (total && guess.episode > total)) break;
    if (guess.at >= from) found.push(guess);
  }
  return found;
}

// A week Monday to Sunday, `offset` weeks from the current one.
export function buildWeek(shows, today, offset = 0) {
  const dayIndex = (today.getDay() + 6) % 7;
  const monday = new Date(today.getFullYear(), today.getMonth(), today.getDate() - dayIndex + 7 * offset);
  const sunday = new Date(monday.getFullYear(), monday.getMonth(), monday.getDate() + 6, 23, 59, 59, 999);
  const startOfToday = new Date(today.getFullYear(), today.getMonth(), today.getDate());
  const airing = shows.flatMap(show => airingsBetween(show, monday, sunday).map(a => ({
    show, at: a.at, episode: a.episode, passed: a.at <= today,
    done: a.done ?? (show.last_done || 0) >= a.episode,
    minutes: a.at.getHours() * 60 + a.at.getMinutes(),
  })));
  const days = [];
  for (let d = 0; d < 7; d++) {
    const date = new Date(monday.getFullYear(), monday.getMonth(), monday.getDate() + d);
    const entries = airing.filter(entry => entry.at.toDateString() === date.toDateString());
    entries.sort((a, b) => a.minutes - b.minutes);
    const slots = [];
    entries.forEach(entry => {
      const last = slots[slots.length - 1];
      if (last && last.minutes === entry.minutes) last.items.push(entry);
      else slots.push({ minutes: entry.minutes, label: time(entry.at), passed: entry.passed, items: [entry] });
    });
    days.push({
      date, slots, today: date.getTime() === startOfToday.getTime(), past: date < startOfToday,
      name: date.toLocaleDateString('en-US', { weekday: 'short' }),
      label: date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }),
    });
  }
  const range = `${monday.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })} – ${sunday.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })}`;
  return { days, range };
}

function Slot({ slot }) {
  return html`<div class="cal-slot">
    <div class=${`cal-time${slot.passed ? ' passed' : ''}`}>${slot.label}</div>
    <div class="cal-items">
      ${slot.items.map(({ show, passed, episode, done }) => html`<button type="button" key=${show.id} class=${`cal-item${show.paused ? ' paused' : passed ? ' passed' : ''}`} onPointerEnter=${() => prefetch(show.id)} onClick=${() => go(show.id)}>
        <div class="cal-poster"><${Poster} show=${show} dim=${show.paused} /></div>
        <div class="cal-info">
          <h4 class="clamp-3" title=${show.title}>${show.title}</h4>
          <div class="row wrap"><span class="ep">Ep ${episode}</span>${passed && done ? html`<span class="ep ok" title="On disk"><${Icon} name="check" small /></span>` : null}<${StatusTag} show=${show} /></div>
        </div>
      </button>`)}
    </div>
  </div>`;
}

export function Calendar() {
  const { data } = useResource(showsData);
  const now = useNow();
  const minute = Math.floor(now / 60000);
  const [offset, setOffset] = useState(0);
  const week = useMemo(() => buildWeek(data || [], new Date(minute * 60000), offset), [data, minute, offset]);
  const nowLine = html`<div class="cal-now">Now</div>`;
  return html`<div class="page full">
    <div class="cal-nav">
      <button type="button" class="btn plain icon-only sm" onClick=${() => setOffset(offset - 1)} aria-label="Previous week" title="Previous week"><${Icon} name="back" /></button>
      <button type="button" class="cal-range page-sub" onClick=${() => setOffset(0)} disabled=${offset === 0} title=${offset === 0 ? null : 'Back to this week'}>${week.range}</button>
      <button type="button" class="btn plain icon-only sm" onClick=${() => setOffset(offset + 1)} aria-label="Next week" title="Next week"><${Icon} name="forward" /></button>
    </div>
    <div class="cal-grid">
      ${week.days.map(day => {
        let drawn = false;
        const parts = [];
        day.slots.forEach(slot => {
          if (day.today && !drawn && !slot.passed) { parts.push(nowLine); drawn = true; }
          parts.push(html`<${Slot} slot=${slot} key=${slot.minutes} />`);
        });
        if (day.today && !drawn) parts.push(nowLine);
        return html`<div class=${`cal-day${day.past ? ' past' : ''}`} key=${day.label}>
          <div class=${`cal-head${day.today ? ' today' : ''}`}><span class="section-title">${day.name}</span><span class="muted">${day.label}</span></div>
          <div class=${`cal-rail${day.today ? ' today' : ''}`}>${parts}</div>
        </div>`;
      })}
    </div>
  </div>`;
}
