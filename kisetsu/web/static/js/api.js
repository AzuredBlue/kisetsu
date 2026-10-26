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

// `json` is sent as the body. With `quiet` a failure is thrown but not shown.
export async function apiFetch(path, { method = 'GET', json, quiet = false } = {}) {
  const options = { method, headers: {} };
  if (json !== undefined) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(json);
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch (err) {
    const error = new ApiError('Kisetsu did not answer.', 0, null);
    if (!quiet) onError(error.message);
    throw error;
  }
  let body = null;
  try { body = await response.json(); } catch (err) { /* no body */ }
  if (!response.ok) {
    const message = detailText(body && body.detail) || `Request failed (${response.status}).`;
    const error = new ApiError(message, response.status, body);
    if (!quiet) onError(message);
    throw error;
  }
  return body;
}
