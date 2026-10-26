// The server data the views read.
import { keyedResource, resource } from './store.js';

export const shows = resource('/api/shows', ['shows']);
export const status = resource('/api/status', ['status', 'shows', 'feeds']);
export const feeds = resource('/api/feeds', ['feeds', 'shows']);
export const settings = resource('/api/settings', ['settings']);
export const history = resource('/api/history?limit=100', ['history']);
export const logs = resource('/api/logs?limit=300', ['logs']);
export const setupStatus = resource('/api/setup/status', ['settings', 'status']);
export const showPage = (id) => keyedResource(`/api/shows/${id}`, [`show:${id}`, 'shows']);
export const feedMatches = (id) => keyedResource(`/api/shows/${id}/feed-matches`, [`show:${id}`, 'shows', 'feeds']);
export const feedItems = (query) => keyedResource(`/api/feeds/items?${query}`, ['feed_items', 'feeds', 'shows']);
