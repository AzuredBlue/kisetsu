// Edits to a form kept apart from the saved values, so a refresh from the server never overwrites what is being typed.
import { useEffect, useState } from '../ui.js';

export function useDraft(initial, resetKey) {
  const [draft, setDraft] = useState({});
  useEffect(() => setDraft({}), [resetKey]);
  const value = (key) => (key in draft ? draft[key] : initial[key]);
  const set = (key, v) => setDraft(d => ({ ...d, [key]: v }));
  const changed = Object.keys(draft).filter(key => String(draft[key]) !== String(initial[key]));
  return {
    value,
    set,
    // Props for a text or number input.
    bind: (key) => ({ value: value(key), onInput: (e) => set(key, e.target.value) }),
    dirty: changed.length > 0,
    changed: () => Object.fromEntries(changed.map(key => [key, draft[key]])),
    reset: () => setDraft({}),
  };
}
