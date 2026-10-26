import { html, useLayoutEffect, useRef } from '../ui.js';
import { ACCENT_PRESETS } from '../color.js';
import { Icon } from './Icon.js';

// A row of mutually exclusive choices. `options` are [value, label] or [value, label, icon].
export function Seg({ value, options, onChange, small = false, icons = false, label }) {
  const box = useRef(null);
  // The highlight is one element that slides to the pressed button.
  useLayoutEffect(() => {
    const el = box.current;
    const place = () => {
      const on = el.querySelector('[aria-pressed="true"]');
      if (!on) { el.style.setProperty('--thumb-w', '0px'); return; }
      el.style.setProperty('--thumb-x', `${on.offsetLeft}px`);
      el.style.setProperty('--thumb-w', `${on.offsetWidth}px`);
      requestAnimationFrame(() => el.classList.add('placed'));
    };
    place();
    const watch = new ResizeObserver(place);
    watch.observe(el);
    return () => watch.disconnect();
  }, [value, options.length]);
  return html`<div ref=${box} class=${`seg${small ? ' sm' : ''}${icons ? ' icons' : ''}`} role="group" aria-label=${label}>
    ${options.map(([v, text, icon, title]) => html`<button type="button" key=${v} aria-pressed=${String(value === v)} title=${title} aria-label=${icons ? text : undefined} onClick=${() => onChange(v)}>
      ${icon ? html`<${Icon} name=${icon} small />` : null}${icons ? null : html`<span>${text}</span>`}
    </button>`)}
  </div>`;
}

// The accent presets and a custom colour picker.
export function Swatches({ value, onPick, onPreview }) {
  const preset = ACCENT_PRESETS.some(p => p[1] === value);
  return html`<div class="swatches">
    ${ACCENT_PRESETS.map(([name, hex]) => html`<button type="button" class="swatch" key=${hex} title=${name} aria-label=${name} aria-pressed=${String(hex === value)} style=${`background:${hex}`} onClick=${() => onPick(hex)}></button>`)}
    <label class=${`swatch${preset ? '' : ' on'}`} title="Custom colour" style=${`background:${preset ? 'conic-gradient(#fb7185, #fbbf24, #a3e635, #2dd4bf, #818cf8, #fb7185)' : value}`}>
      <input type="color" value=${value} aria-label="Custom colour" onInput=${(e) => onPreview && onPreview(e.target.value)} onChange=${(e) => onPick(e.target.value)} />
    </label>
  </div>`;
}

export function SaveBar({ open, onDiscard, onSave, saving = false, label = 'Save changes' }) {
  return html`<div class="save-bar-slot">
    <div class=${`save-bar${open ? ' open' : ''}`} inert=${!open} aria-hidden=${String(!open)}>
      <span>Unsaved changes</span>
      <div class="actions">
        <button type="button" class="btn sm" onClick=${onDiscard} disabled=${saving}>Discard</button>
        <button type="button" class="btn sm primary" onClick=${onSave} disabled=${saving}>${saving ? 'Saving…' : label}</button>
      </div>
    </div>
  </div>`;
}

// One labelled settings line: name and hint on the left, the control on the right.
export function SetRow({ name, hint, htmlFor, size, children }) {
  return html`<div class="set-row">
    <div class="set-text">
      ${htmlFor ? html`<label class="set-name" for=${htmlFor}>${name}</label>` : html`<div class="set-name">${name}</div>`}
      ${hint ? html`<p class="field-hint">${hint}</p>` : null}
    </div>
    <div class=${`set-ctl${size ? ` ${size}` : ''}`}>${children}</div>
  </div>`;
}
