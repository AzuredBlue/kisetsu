import { html } from '../ui.js';

// What every page shows when it has nothing to list: what is missing, and what to do about it.
export function EmptyState({ title, hint = null, children = null, card = false }) {
  return html`<div class=${`empty${card ? ' card' : ''}`}>
    <div class="empty-title">${title}</div>
    ${hint ? html`<p class="field-hint">${hint}</p>` : null}
    ${children}
  </div>`;
}
