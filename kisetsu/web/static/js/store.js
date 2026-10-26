// Server data lives here. A resource is fetched when a view first uses it and again whenever the
// server says (over the event stream) that its topic changed. Nothing polls.
import { useEffect, useState } from './ui.js';
import { apiFetch } from './api.js';

const listeners = new Set();
let connection = { online: true };

export class Resource {
  // `topics` are the stream topics that make the data stale; a function gives them for a keyed resource.
  constructor(path, topics) {
    this.path = path;
    this.topics = topics;
    this.data = null;
    this.at = 0;
    this.stale = true;
    this.users = 0;
    this.inflight = null;
    this.again = false;
  }

  async load() {
    if (this.inflight) { this.again = true; return this.inflight; }
    this.stale = false;
    this.inflight = apiFetch(this.path, { quiet: true })
      .then((data) => { this.data = data; this.at = Date.now(); this.error = null; })
      .catch((err) => { this.error = err; this.stale = true; })
      .finally(() => {
        this.inflight = null;
        notify();
        if (this.again) { this.again = false; this.load(); }
      });
    return this.inflight;
  }

  invalidate() {
    this.stale = true;
    if (this.users > 0) this.load();
  }
}

const resources = new Set();
const keyed = new Map();

export function resource(path, topics) {
  const r = new Resource(path, topics);
  resources.add(r);
  return r;
}

// One resource per key (a show's id, say); dropped from the cache when nobody uses it any more.
export function keyedResource(path, topics) {
  const key = path;
  let r = keyed.get(key);
  if (!r) {
    r = new Resource(path, topics);
    keyed.set(key, r);
    resources.add(r);
  }
  return r;
}

function notify() { listeners.forEach(fn => fn()); }

export function useResource(r) {
  const [, rerender] = useState(0);
  useEffect(() => {
    const fn = () => rerender(n => n + 1);
    listeners.add(fn);
    r.users += 1;
    if (r.stale) r.load();
    return () => {
      listeners.delete(fn);
      r.users -= 1;
      if (r.users === 0 && keyed.get(r.path) === r) { keyed.delete(r.path); resources.delete(r); }
    };
  }, [r]);
  return { data: r.data, at: r.at, loading: r.data === null && !r.error, error: r.error || null, reload: () => r.load() };
}

export function useConnection() {
  const [, rerender] = useState(0);
  useEffect(() => {
    const fn = () => rerender(n => n + 1);
    listeners.add(fn);
    return () => listeners.delete(fn);
  }, []);
  return connection;
}

function matches(r, topics) {
  if (topics.includes('*')) return true;
  const mine = typeof r.topics === 'function' ? r.topics() : r.topics;
  return mine.some(t => topics.includes(t));
}

export function invalidate(topics) {
  resources.forEach(r => { if (matches(r, topics)) r.invalidate(); });
}

// Opens the event stream. The server sends "*" first on every (re)connect, which refetches whatever is in use.
export function connect() {
  const source = new EventSource('/api/stream');
  source.onmessage = (event) => {
    let message;
    try { message = JSON.parse(event.data); } catch (err) { return; }
    if (!connection.online) { connection = { online: true }; notify(); }
    invalidate(message.changed || ['*']);
  };
  source.onerror = () => {
    if (connection.online) { connection = { online: false }; notify(); }
  };
  return source;
}

// Refetch now, whatever the topic: for a button the user presses.
export function refetch(r) { return r.load(); }
