// Times arrive as UTC ISO strings; they are shown in the viewer's own timezone.

const toDate = (iso) => {
  if (!iso) return null;
  const d = new Date(iso);
  return isNaN(d) ? null : d;
};

export function localTime(iso, seconds = false) {
  const d = toDate(iso);
  if (!d) return '';
  return d.toLocaleTimeString([], seconds
    ? { hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' }
    : { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
}

export function localDateTime(iso) {
  const d = toDate(iso);
  if (!d) return '';
  return d.toLocaleString([], { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
}

export function localDate(iso) {
  const d = toDate(iso);
  if (!d) return '';
  return d.toLocaleDateString([], { day: 'numeric', month: 'short' });
}

// "2d 1h" until the time, or "Aired" once it has passed; null without a time.
export function countdown(iso, now = Date.now()) {
  const d = toDate(iso);
  if (!d) return null;
  const seconds = Math.floor((d.getTime() - now) / 1000);
  if (seconds <= 0) return 'Aired';
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days >= 7) return `${days}d`;
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${Math.max(1, minutes)}m`;
}

export function relativeTime(iso, now = Date.now()) {
  const d = toDate(iso);
  if (!d) return '';
  const seconds = Math.max(0, Math.floor((now - d.getTime()) / 1000));
  if (seconds < 60) return 'just now';
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

export function dayLabel(iso, now = new Date()) {
  const d = toDate(iso);
  if (!d) return 'Earlier';
  const startOf = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const days = Math.round((startOf(now) - startOf(d)) / 86400000);
  if (days <= 0) return 'Today';
  if (days === 1) return 'Yesterday';
  return d.toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long' });
}

// "in 4m 12s" for a wait of `seconds`.
export function wait(seconds) {
  const total = Math.max(0, Math.floor(seconds));
  if (total <= 0) return 'now';
  if (total < 60) return `in ${total}s`;
  const minutes = Math.floor(total / 60);
  if (minutes < 60) return `in ${minutes}m ${total % 60}s`;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return m > 0 ? `in ${h}h ${m}m` : `in ${h}h`;
}

// How full a show's progress bar is. With a known season total it is done/total; while the total is
// unknown it is done out of the episodes out so far plus the one still to come, so a running show
// never looks finished and a backlog shows.
export function progressFraction({ done, aired, total }) {
  if (total > 0) return Math.min(1, done / total);
  return Math.min(1, done / (Math.max(aired, done) + 1));
}

export function initials(name) {
  const words = (name || '').split(/\s+/).filter(Boolean);
  return words.slice(0, 2).map(w => w[0]).join('').toUpperCase() || '?';
}

export function plural(n, one, many = `${one}s`) {
  return `${n} ${n === 1 ? one : many}`;
}
