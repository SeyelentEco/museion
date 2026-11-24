// How the page talks to Museion (museion/app.py).
//
// invoke('ask', { question }) runs a command and resolves with its result,
// or rejects with the error as a sentence for the person. listen('name', fn)
// calls fn({ payload }) for every event of that name until the function it
// returns is called.

export async function invoke(command, args = {}) {
  let response;
  try {
    response = await fetch(`/api/${command}`, {
      method: 'POST',
      // The header is what tells Museion the request came from this page:
      // another site's page cannot send it.
      headers: { 'Content-Type': 'application/json', 'X-Museion': '1' },
      body: JSON.stringify(args),
    });
  } catch {
    throw 'Museion is not running. Start it again to carry on.';
  }
  let body = null;
  try {
    body = await response.json();
  } catch {
    throw `Museion gave no answer to ${command} (status ${response.status}).`;
  }
  // {"ok": result} or {"error": "a sentence for the person"}.
  if (body && 'ok' in body) return body.ok;
  throw body?.error ?? `${command} failed (status ${response.status}).`;
}

// One stream for all events; the browser reconnects it by itself after a
// dropped connection or a restart of Museion.
const events = new EventSource('/api/events');

export function listen(name, handler) {
  const h = (e) => handler({ payload: JSON.parse(e.data) });
  events.addEventListener(name, h);
  return () => events.removeEventListener(name, h);
}

export function closeEvents() {
  events.close();
}
