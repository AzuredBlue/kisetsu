import { html, useMemo } from '../ui.js';
import { useResource } from '../store.js';
import { go, prefetch, useNow } from '../router.js';
import { shows as showsData } from '../data.js';
import { Poster, StatusTag } from '../components/ShowSummary.js';

const time = (date) => date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });

// The week Monday to Sunday: a released show repeats on its air day, an unreleased one appears once on its premiere date.
export function buildWeek(shows, today) {
  const dayIndex = (today.getDay() + 6) % 7;
  const nowMinutes = today.getHours() * 60 + today.getMinutes();
  const monday = new Date(today.getFullYear(), today.getMonth(), today.getDate() - dayIndex);
  const sunday = new Date(monday.getFullYear(), monday.getMonth(), monday.getDate() + 6, 23, 59, 59, 999);
  const days = [];
  for (let d = 0; d < 7; d++) {
    const date = new Date(monday.getFullYear(), monday.getMonth(), monday.getDate() + d);
    const entries = [];
    shows.forEach(show => {
      if (!show.next_airing) return;
      const base = new Date(show.next_airing.at);
      if (isNaN(base)) return;
      let at = null;
      if (show.released) {
        if (base.getDay() === date.getDay()) { at = new Date(date); at.setHours(base.getHours(), base.getMinutes(), 0, 0); }
      } else if (base >= monday && base <= sunday && base.toDateString() === date.toDateString()) {
        at = base;
      }
      if (!at) return;
      const minutes = base.getHours() * 60 + base.getMinutes();
      entries.push({ show, minutes, at, passed: d < dayIndex || (d === dayIndex && minutes <= nowMinutes) });
    });
    entries.sort((a, b) => a.minutes - b.minutes);
    const slots = [];
    entries.forEach(entry => {
      const last = slots[slots.length - 1];
      if (last && last.minutes === entry.minutes) last.items.push(entry);
      else slots.push({ minutes: entry.minutes, label: time(entry.at), passed: entry.passed, items: [entry] });
    });
    days.push({
      date, slots, today: d === dayIndex, past: d < dayIndex,
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
      ${slot.items.map(({ show, passed }) => html`<button type="button" key=${show.id} class=${`cal-item${show.paused ? ' paused' : passed ? ' passed' : ''}`} onPointerEnter=${() => prefetch(show.id)} onClick=${() => go(show.id)}>
        <div class="cal-poster"><${Poster} show=${show} dim=${show.paused} /></div>
        <div class="cal-info">
          <h4 class="clamp-3" title=${show.title}>${show.title}</h4>
          <div class="row wrap"><span class="ep">Ep ${show.next_airing.episode || show.last_done || 1}</span><${StatusTag} show=${show} /></div>
        </div>
      </button>`)}
    </div>
  </div>`;
}

export function Calendar() {
  const { data } = useResource(showsData);
  const now = useNow();
  const minute = Math.floor(now / 60000);
  const week = useMemo(() => buildWeek(data || [], new Date(minute * 60000)), [data, minute]);
  const nowLine = html`<div class="cal-now">Now</div>`;
  return html`<div class="page full">
    <p class="page-sub">${week.range}</p>
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
