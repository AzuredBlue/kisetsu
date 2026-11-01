import { html, useEffect, useRef, useState } from '../ui.js';
import { Icon } from './Icon.js';

let open = () => {};

// Shows a modal built by `build(close)`; resolves with whatever `close` is called with.
export function showModal(build, { wide = false } = {}) {
  return new Promise(resolve => open({ build, wide, resolve }));
}

export function confirmDialog({ title, body, confirm = 'Confirm', danger = false, cancel = 'Cancel', icon = null, wide = false }) {
  return showModal((close) => html`
    ${icon ? html`<div class="modal-head"><span class=${`modal-badge${danger ? ' danger' : ''}`}><${Icon} name=${icon} /></span><h2>${title}</h2></div>` : html`<h2>${title}</h2>`}
    <div class="body">${body}</div>
    <div class="actions">
      <button class="btn" onClick=${() => close(false)}>${cancel}</button>
      <button class=${`btn ${danger ? 'danger' : 'primary'}`} onClick=${() => close(true)}>${confirm}</button>
    </div>`, { wide });
}

export function ModalHost() {
  const [modal, setModal] = useState(null);
  const panel = useRef(null);
  useEffect(() => { open = setModal; return () => { open = () => {}; }; }, []);
  useEffect(() => {
    if (!modal) return undefined;
    const previous = document.activeElement;
    const focusable = panel.current && panel.current.querySelector('input, button.primary, button.danger, button');
    if (focusable) focusable.focus();
    const onKey = (e) => { if (e.key === 'Escape') close(undefined); };
    document.addEventListener('keydown', onKey);
    return () => { document.removeEventListener('keydown', onKey); if (previous && previous.focus) previous.focus(); };
  }, [modal]);
  if (!modal) return null;
  const close = (value) => { setModal(null); modal.resolve(value); };
  return html`<div class="modal-backdrop" onMouseDown=${(e) => { if (e.target === e.currentTarget) close(undefined); }}>
    <div class=${`modal${modal.wide ? ' wide' : ''}`} role="dialog" aria-modal="true" ref=${panel}>${modal.build(close)}</div>
  </div>`;
}

