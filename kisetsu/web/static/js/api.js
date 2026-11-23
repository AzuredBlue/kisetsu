// Every request goes through apiFetch, so an error always reaches the user instead of vanishing.

export class ApiError extends Error {
  constructor(message, status, body) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

let onError = (message) => console.error(message);
export function setErrorHandler(fn) { onError = fn; }

function detailText(detail) {
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) return detail.map(d => d.msg || JSON.stringify(d)).join('; ');
  return detail ? JSON.stringify(detail) : '';
}

// `json` is sent as the body, or `file` as it is. With `quiet` a failure is thrown but not shown.
export async function apiFetch(path, { method = 'GET', json, file, headers = {}, quiet = false } = {}) {
  const options = { method, headers: { ...headers } };
  if (json !== undefined) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(json);
  }
  if (file !== undefined) options.body = file;
  const response = await send(path, options, quiet);
  try { return await response.json(); } catch (err) { return null; }
}

// Saves what the server sends as a file, under the name it gives.
export async function apiDownload(path) {
  const response = await send(path, {}, false);
  const name = /filename="?([^";]+)/.exec(response.headers.get('Content-Disposition') || '');
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement('a');
  link.href = url;
  link.download = name ? name[1] : 'kisetsu-download';
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}

// How long an answer the page asked for before the scripts loaded (index.html) may stand in for a request.
const EARLY_FOR_MS = 5000;

// When the page asked for `path` early, if it did and that answer is still to be taken.
export function earlyAt(path) {
  const early = window.__kisetsuEarly;
  return early && early[path] ? early[path].at : null;
}

// The page's own early request for `path`, once: only for a plain GET, and only while it is recent.
function takeEarly(path, options) {
  const early = window.__kisetsuEarly;
  const found = early && early[path];
  if (!found) return null;
  delete early[path];
  if (options.method !== 'GET' || options.body !== undefined || Date.now() - found.at > EARLY_FOR_MS) return null;
  return found.answer;
}

async function send(path, options, quiet) {
  let response;
  try {
    response = await (takeEarly(path, options) || fetch(path, options));
  } catch (err) {
    const error = new ApiError('Kisetsu did not answer.', 0, null);
    if (!quiet) onError(error.message);
    throw error;
  }
  if (response.ok) return response;
  let body = null;
  try { body = await response.json(); } catch (err) { /* no body */ }
  const message = detailText(body && body.detail) || `Request failed (${response.status}).`;
  const error = new ApiError(message, response.status, body);
  if (!quiet) onError(message);
  throw error;
}
