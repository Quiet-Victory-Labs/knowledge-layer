import assert from 'node:assert/strict';
import test from 'node:test';

let moduleId = 0;
const freshApi = () => import(`../src/platformApi.ts?auth-test=${++moduleId}`);
const demoSession = { mode: 'demo', subject: 'operator', groups: [], steward: true, csrf_token: null };
const source = { id: 'source', name: 'Files', kind: 'local_files' };
const input = { name: 'Files', kind: 'local_files', config: { path: '.' } };
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { 'Content-Type': 'application/json' },
});

test('explicit demo session permits browser writes without fabricating a request token', async (t) => {
  const api = await freshApi();
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  t.mock.method(globalThis, 'fetch', async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    return json(url === '/auth/me' ? demoSession : { connection: source });
  });
  assert.deepEqual(await api.getBrowserSession(), demoSession);
  assert.equal(await api.getStewardRole(), true);
  assert.equal((await api.createConnection(input)).id, 'source');
  assert.deepEqual(calls.map(({ url }) => url), ['/auth/me', '/auth/me', '/api/knowledge/connections']);
  const mutation = calls.at(-1)!.init!;
  assert.equal(new Headers(mutation.headers).has('X-Knowledge-CSRF'), false);
  assert.equal(mutation.credentials, 'same-origin');
});

test('first demo mutation loads and caches the explicit demo session', async (t) => {
  const api = await freshApi();
  const urls: string[] = [];
  t.mock.method(globalThis, 'fetch', async (url: string) => {
    urls.push(url);
    return json(url === '/auth/me' ? demoSession : { connection: source });
  });
  await api.createConnection(input);
  await api.createConnection(input);
  assert.deepEqual(urls, ['/auth/me', '/api/knowledge/connections', '/api/knowledge/connections']);
});

for (const mode of [undefined, 'local', 'oidc', 'iap']) {
  test(`${mode ?? 'existing managed'} session sends its request token on mutations`, async (t) => {
    const api = await freshApi();
    const requestToken = crypto.randomUUID();
    t.mock.method(globalThis, 'fetch', async (url: string, init?: RequestInit) => {
      if (url === '/auth/me') {
        return json({ mode, subject: 'owner', groups: [], steward: false, csrf_token: requestToken });
      }
      assert.equal(new Headers(init?.headers).get('X-Knowledge-CSRF'), requestToken);
      return json({ connection: source });
    });
    await api.createConnection(input);
    assert.equal(await api.getStewardRole(), false);
  });
}

for (const session of [
  { subject: 'owner', steward: true, csrf_token: null },
  { mode: 'local', subject: 'owner', steward: true, csrf_token: null },
  { mode: 'oidc', subject: 'owner', steward: true },
  { mode: 'iap', subject: 'owner', steward: true, csrf_token: '' },
  { mode: 'demo', subject: 'operator', steward: true },
]) {
  test(`missing token is refused unless explicitly null in demo: ${JSON.stringify(session)}`, async (t) => {
    const api = await freshApi();
    const urls: string[] = [];
    t.mock.method(globalThis, 'fetch', async (url: string) => {
      urls.push(url);
      return json(session);
    });
    await assert.rejects(api.getStewardRole(), { name: 'PlatformApiError', status: 403 });
    await assert.rejects(api.createConnection(input), { name: 'PlatformApiError', status: 403 });
    assert.deepEqual(urls, ['/auth/me', '/auth/me']);
  });
}

test('missing auth route cannot imply demo authority or permit a browser mutation', async (t) => {
  const api = await freshApi();
  const urls: string[] = [];
  t.mock.method(globalThis, 'fetch', async (url: string) => {
    urls.push(url);
    return json({ detail: 'Not Found' }, 404);
  });
  await assert.rejects(api.getStewardRole(), { name: 'PlatformApiError', status: 404 });
  await assert.rejects(api.createConnection(input), { name: 'PlatformApiError', status: 404 });
  assert.deepEqual(urls, ['/auth/me', '/auth/me']);
});

test('expired managed session notifies the gate and prevents its mutation', async (t) => {
  const api = await freshApi();
  const target = new EventTarget();
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'window');
  Object.defineProperty(globalThis, 'window', { configurable: true, value: target });
  t.after(() => {
    if (previous) Object.defineProperty(globalThis, 'window', previous);
    else Reflect.deleteProperty(globalThis, 'window');
  });
  let expired = 0;
  target.addEventListener('knowledge-session-expired', () => { expired += 1; });
  const urls: string[] = [];
  t.mock.method(globalThis, 'fetch', async (url: string) => {
    urls.push(url);
    return json({ detail: 'Sign in required' }, 401);
  });
  await assert.rejects(api.createConnection(input), { name: 'PlatformApiError', status: 401 });
  assert.equal(expired, 1);
  assert.deepEqual(urls, ['/auth/me']);
});
