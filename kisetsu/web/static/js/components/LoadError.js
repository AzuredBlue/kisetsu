import { html } from '../ui.js';

// Shown where a view has nothing to draw because its fetch failed, so a failure never reads as "nothing here".
// Stale data is kept on screen instead (the header says when the server stopped answering).
export function LoadError({ what, error, reload }) {
  return html`<div class="empty load-error" role="alert">
    <div>Could not load ${what}.</div>
    ${error && error.message ? html`<p class="field-hint">${error.message}</p>` : null}
    <button type="button" class="btn sm" onClick=${reload}>Retry</button>
  </div>`;
}
