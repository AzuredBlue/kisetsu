import { html, useEffect, useState } from '../ui.js';
import { Icon } from './Icon.js';

let push = () => {};
const ICONS = { error: 'alert', success: 'check', info: 'info' };
const LIFETIME_MS = 3500;
let nextId = 1;

export function toast(message, type = 'info') {
  push({ id: nextId++, message, type });
}

export function Toasts() {
  const [items, setItems] = useState([]);
  useEffect(() => {
    push = (item) => {
      setItems(list => [...list, item]);
      if (item.type !== 'error') setTimeout(() => setItems(list => list.filter(t => t.id !== item.id)), LIFETIME_MS);
    };
    return () => { push = () => {}; };
  }, []);
  const close = (id) => setItems(list => list.filter(t => t.id !== id));
  return html`<div class="toasts">
    ${items.map(t => html`<div class=${`toast ${t.type}`} role=${t.type === 'error' ? 'alert' : 'status'} key=${t.id}>
      <${Icon} name=${ICONS[t.type] || 'info'} />
      <p>${t.message}</p>
      <button class="btn plain icon-only sm" aria-label="Dismiss" onClick=${() => close(t.id)}><${Icon} name="x" small /></button>
    </div>`)}
  </div>`;
}
