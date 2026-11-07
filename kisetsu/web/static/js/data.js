// The server data the views read.
import { keyedResource, resource } from './store.js';

// 'shows' is any change to the shows; 'episodes' is one only to their episodes and downloads, which also names the show
// (`show:<id>`), so the feeds and another show's page are not refetched for it.
export const shows = resource('/api/shows', ['shows', 'episodes']);
export const status = resource('/api/status', ['status', 'shows', 'episodes', 'feeds']);
export const feeds = resource('/api/feeds', ['feeds', 'shows']);
export const settings = resource('/api/settings', ['settings']);
export const history = resource('/api/history?limit=100', ['history']);
// Episodes across every show that need the user's eye (Shows page).
export const attention = resource('/api/attention', ['shows', 'episodes', 'history']);
export const logs = resource('/api/logs?limit=300', ['logs']);
// Requests sent to each site since start (Logs page, sandbox only); the server says when they change, every few seconds at most.
export const requests = resource('/api/requests', ['requests']);
// Changes only when setup is finished (which emits 'settings') or a backup is restored (which emits everything).
export const setupStatus = resource('/api/setup/status', ['settings']);
export const showPage = (id) => keyedResource(`/api/shows/${id}`, [`show:${id}`, 'shows']);
export const feedMatches = (id) => keyedResource(`/api/shows/${id}/feed-matches`, [`show:${id}`, 'shows', 'feeds']);
export const feedItems = (query) => keyedResource(`/api/feeds/items?${query}`, ['feed_items', 'feeds', 'shows', 'episodes']);
