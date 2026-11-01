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

async function send(path, options, quiet) {
  let response;
  try {
    response = await fetch(path, options);
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
