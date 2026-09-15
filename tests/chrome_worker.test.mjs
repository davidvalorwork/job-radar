import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import {checkRequest, errorReport, pairingConfig, sendArguments, unwrapDOM} from '../chrome-extension/protocol.mjs';
import {gmailDOM} from '../chrome-extension/gmail-dom.mjs';
import {originalTabs, originalTabFailure} from '../chrome-extension/original-tabs.mjs';
import {WORKER_VERSION} from '../chrome-extension/protocol.mjs';

const pairing = {url: 'http://127.0.0.1:49152', token: 'a'.repeat(43), account: 'test@example.com'};
test('pairing accepts loopback only and rejects credentials in URL', () => {
  assert.deepEqual(pairingConfig(pairing), pairing);
  for (const url of ['https://example.com', 'http://localhost:3', 'http://127.0.0.1:3/x',
    'http://evil@127.0.0.1:3', 'http://127.0.0.1:3?token=1', 'http://127.0.0.1:3#x']) {
    assert.throws(() => pairingConfig({...pairing, url}));
  }
  assert.throws(() => pairingConfig({...pairing, token: 'short'}));
});
test('request has finite deadline, narrow method and UUID', () => {
  const request = {id: 'a'.repeat(32), method: 'gmail_get_profile', arguments: {}, expires_at: 20};
  checkRequest(request, 19000);
  checkRequest({...request, method: 'gmail_create_draft'}, 19000);
  for (const change of [{id: '../file'}, {method: 'eval'}, {expires_at: Infinity}, {expires_at: 19}]) {
    assert.throws(() => checkRequest({...request, ...change}, 19000));
  }
});
test('original MIME is read from Gmail\'s visible raw-message element without a download', async () => {
  const raw = 'Message-ID: <fixture@example.com>\r\nDate: Mon, 14 Sep 2026 12:00:00 +0000\r\n\r\nfixture';
  const savedDocument = Object.getOwnPropertyDescriptor(globalThis, 'document');
  const visibleRaw = {innerText: raw, getClientRects: () => [{}]};
  Object.defineProperty(globalThis, 'document', {configurable: true, value: {
    querySelectorAll: selector => selector === 'pre#raw_message_text' ? [visibleRaw] : [],
  }});
  try {
    assert.deepEqual(await gmailDOM('visibleOriginal'), {raw});
  } finally {
    if (savedDocument) Object.defineProperty(globalThis, 'document', savedDocument);
    else delete globalThis.document;
  }
});
test('visible original fails closed when the MIME is incomplete', async () => {
  const savedDocument = Object.getOwnPropertyDescriptor(globalThis, 'document');
  const visibleRaw = {innerText: 'Message-ID: <fixture@example.com>', getClientRects: () => [{}]};
  Object.defineProperty(globalThis, 'document', {configurable: true, value: {
    querySelectorAll: selector => selector === 'pre#raw_message_text' ? [visibleRaw] : [],
  }});
  try {
    await assert.rejects(gmailDOM('visibleOriginal'), /invalid_original/);
  } finally {
    if (savedDocument) Object.defineProperty(globalThis, 'document', savedDocument);
    else delete globalThis.document;
  }
});
test('send payload excludes header injection, multiple recipients and non-PDF payloads', () => {
  const value = {to: 'jobs@example.com', from_address: 'test@example.com', subject: 'Application', payload: {parts: [
    {mime_type: 'text/plain', body: {content: 'Synthetic test'}},
    {mime_type: 'application/pdf', filename: 'CV.pdf', body: {base64_url_content: 'JVBERi0='}},
  ]}};
  assert.equal(sendArguments(value).to, 'jobs@example.com');
  assert.throws(() => sendArguments({...value, to: 'a@example.com,b@example.com'}));
  assert.throws(() => sendArguments({...value, subject: 'Job\r\nBcc: x@example.com'}));
  assert.throws(() => sendArguments({...value, payload: {parts: []}}));
  value.payload.parts[1].filename = '../CV.pdf';
  assert.throws(() => sendArguments(value));
});
test('DOM adapter fails before acting when account identity cannot be established', async () => {
  globalThis.document = {querySelectorAll: () => []};
  try {await assert.rejects(gmailDOM('send', {account: 'test@example.com'}), /missing_control/);}
  finally {delete globalThis.document;}
});
test('extension permissions stay constrained to Gmail and localhost', () => {
  const manifest = JSON.parse(readFileSync(new URL('../chrome-extension/manifest.json', import.meta.url)));
  assert.deepEqual(manifest.permissions, ['scripting']);
  assert.deepEqual(manifest.host_permissions, ['https://mail.google.com/*', 'http://127.0.0.1/*']);
  assert.equal(manifest.manifest_version, 3);
});

function searchFixture(nextLabel = 'Resultados siguientes', disabled = false) {
  const node = (attributes = {}, content = '') => ({
    textContent: content, innerText: content,
    getClientRects: () => [{}],
    getAttribute: key => attributes[key] ?? null,
  });
  const profile = node({'aria-label': 'Cuenta de Google: Test (test@example.com)'});
  const span = node({'data-legacy-thread-id': 'abcdef123456',
    'data-legacy-last-message-id': 'abcdef123456'});
  const row = {...node({}, 'Synthetic message'), querySelector: () => span};
  const main = {...node({}, 'Synthetic message'), querySelectorAll: () => [row]};
  const next = node({'aria-label': nextLabel, 'aria-disabled': String(disabled)});
  return {querySelectorAll: selector => ({
    'a[aria-label]': [profile], '[role="main"]': [main],
    '[role="button"],button': [next],
  }[selector] || [])};
}

test('search pagination recognizes Gmail Resultados siguientes, not just Siguiente', async () => {
  globalThis.document = searchFixture();
  try {
    assert.deepEqual(await gmailDOM('page', {account: 'test@example.com'}), {
      rows: [{id: 'abcdef123456', messageId: 'abcdef123456', draft: false}], next: true,
    });
  } finally {delete globalThis.document;}
});

for (const label of ['Resultados siguientes', 'Siguiente', 'Next', 'Next results', 'Next page', 'Older']) {
  test(`pagination recognizes enabled and disabled ${label}`, async () => {
    try {
      for (const disabled of [true, false]) {
        globalThis.document = searchFixture(label, disabled);
        assert.equal((await gmailDOM('page', {account: 'test@example.com'})).next, !disabled);
      }
    } finally {delete globalThis.document;}
  });
}

test('incomplete pagination is a structured error, never a complete empty history', async () => {
  globalThis.document = searchFixture('Unknown control');
  try {
    const result = await gmailDOM('page', {account: 'test@example.com', errorEnvelope: true});
    assert.deepEqual(result, {radarError: {code: 'unknown_pagination', phase: 'page'}});
    assert.throws(() => unwrapDOM([{result}], 'page'), error =>
      error.message === 'unknown_pagination' && error.phase === 'page');
  } finally {delete globalThis.document;}
});

test('executeScript missing results stop the worker with an actionable code', () => {
  for (const result of [undefined, [], [{}], [{error: {message: 'private details'}}],
    [{result: {radarError: {code: 'private@example.com'}}}], [{result: {radarError: {}}}]]) {
    assert.throws(() => unwrapDOM(result, 'search'), /browser_script_failed/);
  }
  assert.deepEqual(unwrapDOM([{result: {rows: [], next: false}}], 'page'), {rows: [], next: false});
});

test('error telemetry never includes arbitrary exception text, addresses or URLs', () => {
  const error = new Error('private@example.com token=secret');
  error.phase = 'https://example.com/private';
  assert.deepEqual(errorReport(error), {error: 'browser_operation_failed', phase: 'dispatch'});
  assert.deepEqual(errorReport({message: 'unknown_pagination', phase: 'search'}),
    {error: 'unknown_pagination', phase: 'search'});
});

test('disabled next-page control is never clicked', async () => {
  globalThis.document = searchFixture('Resultados siguientes', true);
  try {await assert.rejects(gmailDOM('next', {account: 'test@example.com'}), /no_next_page/);}
  finally {delete globalThis.document;}
});

test('Show original matches a newly opened exact-message tab without opener metadata', () => {
  const source = {id: 1, url: 'https://mail.google.com/mail/u/0/#inbox'};
  const message = {domId: '#msg-f:123'};
  const tab = {id: 2, url: 'https://mail.google.com/mail/u/0/?view=om&permmsgid=msg-f:123', status: 'complete'};
  assert.deepEqual(originalTabs([tab], new Set([1]), source, message), [tab]);
});

test('Show original accepts its own worker as opener but rejects unrelated openers', () => {
  const source = {id: 1, workerId: 10, url: 'https://mail.google.com/mail/u/0/#inbox'};
  const message = {domId: '#msg-f:123'};
  const tab = {id: 2, openerTabId: 10,
    url: 'https://mail.google.com/mail/u/0/?view=om&permmsgid=msg-f:123'};
  assert.deepEqual(originalTabs([tab], new Set([1]), source, message), [tab]);
  for (const candidate of [
    {...tab, openerTabId: 11},
    {...tab, url: tab.url.replace('/u/0/', '/u/1/')},
    {...tab, url: tab.url.replace('msg-f:123', 'msg-f:456')},
  ]) assert.deepEqual(originalTabs([candidate], new Set([1]), source, message), []);
  assert.deepEqual(originalTabs([tab], new Set([1, 2]), source, message), []);
});

test('Show original never adopts an old, different-account or unrelated tab', () => {
  const source = {id: 1, url: 'https://mail.google.com/mail/u/0/#inbox'};
  const message = {domId: '#msg-f:123'};
  const tab = {id: 2, url: 'https://mail.google.com/mail/u/0/?view=om&permmsgid=msg-f:123'};
  assert.deepEqual(originalTabs([{...tab, openerTabId: 1}], new Set([1]), source, message), [{...tab, openerTabId: 1}]);
  for (const other of [
    {...tab, id: 1}, {...tab, id: 'not-a-tab'}, {...tab, openerTabId: 77},
    {...tab, url: tab.url.replace('/u/0/', '/u/1/')},
    {...tab, url: tab.url.replace('mail.google.com', 'example.com')},
    {...tab, url: tab.url.replace('msg-f:123', 'msg-f:456')},
    {...tab, url: tab.url.replace('view=om', 'view=att')}, {...tab, url: 'invalid'},
  ]) assert.deepEqual(originalTabs([other], new Set([1]), source, message), []);
  assert.deepEqual(originalTabs([tab], new Set([1, 2]), source, message), []);
  assert.throws(() => originalTabs([tab], new Set(), {...source, url: 'https://example.com'}, message));
});

test('Show original retains multiple matches so the caller blocks ambiguity', () => {
  const source = {id: 1, url: 'https://mail.google.com/mail/u/0/#inbox'};
  const tab = {id: 2, url: 'https://mail.google.com/mail/u/0/?view=om&permmsgid=msg-f:123'};
  assert.equal(originalTabs([tab, {...tab, id: 3}], new Set([1]), source, {domId: '#msg-f:123'}).length, 2);
});

test('return to search recognizes Gmail title-only Back button', async () => {
  const fixture = searchFixture();
  let clicks = 0;
  const back = {textContent: '', getClientRects: () => [{}],
    getAttribute: key => key === 'title' ? 'Volver a Resultados de la búsqueda' : null,
    click: () => {clicks++;}};
  const waitingMain = {textContent: 'Waiting for Gmail', innerText: 'Waiting for Gmail',
    getClientRects: () => [{}], getAttribute: () => null, querySelectorAll: () => []};
  globalThis.document = {querySelectorAll: selector => {
    if (selector === 'a[aria-label]') return fixture.querySelectorAll(selector);
    if (selector === '[role="main"]') return clicks ? fixture.querySelectorAll(selector) : [waitingMain];
    if (selector === 'button,[role="button"],[role="menuitem"],a') return [back];
    return clicks ? fixture.querySelectorAll(selector) : [];
  }};
  try {
    const page = await gmailDOM('back', {account: 'test@example.com'});
    assert.equal(clicks, 1);
    assert.equal(page.rows[0].id, 'abcdef123456');
  } finally {delete globalThis.document;}
});

test('return to search retries a reversible Back click that Gmail initially ignores', async () => {
  const fixture = searchFixture();
  let clicks = 0;
  const back = {textContent: '', getClientRects: () => [{}],
    getAttribute: key => key === 'title' ? 'Volver a Resultados de la búsqueda' : null,
    click: () => {clicks++;}};
  const waitingMain = {textContent: 'Waiting for Gmail', innerText: 'Waiting for Gmail',
    getClientRects: () => [{}], getAttribute: () => null, querySelectorAll: () => []};
  globalThis.document = {querySelectorAll: selector => {
    if (selector === 'a[aria-label]') return fixture.querySelectorAll(selector);
    if (selector === '[role="main"]') return clicks >= 2 ? fixture.querySelectorAll(selector) : [waitingMain];
    if (selector === 'button,[role="button"],[role="menuitem"],a') return [back];
    if (clicks >= 2) return fixture.querySelectorAll(selector);
    return [];
  }};
  try {
    const page = await gmailDOM('back', {account: 'test@example.com', deadline: Date.now() + 1200});
    assert.ok(clicks >= 2);
    assert.equal(page.rows[0].id, 'abcdef123456');
  } finally {delete globalThis.document;}
});
for (const scenario of ['user_stop', 'idle_then_stop', 'transport_failure', 'bridge_halted', 'version_mismatch', 'version_mismatch_send', 'worker_original']) {
test(`reconnecting clears a stale error: ${scenario}`, async () => {
  const elements = new Map();
  const get = id => {
    if (!elements.has(id)) elements.set(id, {
      disabled: false, value: '42', textContent: '', files: [], checked: false,
      replaceChildren() {}, append() {},
    });
    return elements.get(id);
  };
  const saved = new Map(['document', 'chrome', 'fetch', 'navigator'].map(
    key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  const seen = [];
  const replies = [];
  const actions = [];
  const mismatch = scenario.startsWith('version_mismatch');
  let originalOpened = false;
  const closedTabs = [];
  const sourceTab = {id: 42, url: 'https://mail.google.com/mail/u/0/'};
  const originalTab = {id: 77, openerTabId: 99, status: 'complete',
    url: 'https://mail.google.com/mail/u/0/?view=om&permmsgid=msg-f:123'};
  Object.defineProperties(globalThis, {
    document: {configurable: true, value: {getElementById: get, createElement: () => ({})}},
    chrome: {configurable: true, value: {
      tabs: {
        query: async () => originalOpened ? [sourceTab, originalTab] : [sourceTab],
        get: async () => sourceTab, getCurrent: async () => ({id: 99}),
        remove: async id => {closedTabs.push(id); originalOpened = false;},
      },
      runtime: {getManifest: () => ({version: mismatch ? '0.1.0' : WORKER_VERSION})},
      scripting: {executeScript: async input => {
        actions.push(input.args[0]);
        if (scenario === 'worker_original') {
          const result = {
            search: {rows: [{id: 'abcdef12', messageId: 'abcdef12'}], next: false},
            open: [{id: 'abcdef12', domId: '#msg-f:123'}],
            original: true, back: {},
            visibleOriginal: {raw: 'Message-ID: <synthetic@example.com>\r\nDate: Mon, 14 Sep 2026 12:00:00 +0000\r\n\r\nSynthetic fixture'},
          }[input.args[0]];
          if (input.args[0] === 'original') originalOpened = true;
          if (result !== undefined) return [{result}];
        }
        return [{result: {email: 'test@example.com'}}];
      }},
    }},
    navigator: {configurable: true, value: {locks: {request: async (_, __, fn) => fn({})}}},
    fetch: {configurable: true, value: async (url, options) => {
      if (url.startsWith('https://mail.google.com/')) {
        assert.equal(options.credentials, 'include');
        return new Response('Message-ID: <synthetic@example.com>\r\nDate: Mon, 14 Sep 2026 12:00:00 +0000\r\n\r\nSynthetic fixture',
          {headers: {'Content-Type': 'message/rfc822'}});
      }
      if (url.endsWith('/result')) {
        replies.push(JSON.parse(options.body));
        get('stop').onclick();
        return {ok: true, json: async () => ({accepted: true})};
      }
      seen.push(get('status').textContent);
      assert.deepEqual(JSON.parse(options.body), {worker_version: WORKER_VERSION});
      if (scenario === 'worker_original') return {ok: true, json: async () => ({
        account: 'test@example.com', enable_send: false,
        request: {id: 'b'.repeat(32), method: 'gmail_search_emails',
          arguments: {query: 'in:sent after:1'}, expires_at: Date.now() / 1000 + 1},
      })};
      if (mismatch) return {ok: true, json: async () => ({account: 'test@example.com', enable_send: true,
        request: {id: 'a'.repeat(32), arguments: {}, expires_at: Date.now() / 1000 + 60,
          method: scenario.endsWith('_send') ? 'gmail_send_email' : 'gmail_get_profile'},
      })};
      if (scenario === 'transport_failure') throw new Error('private server details');
      if (scenario === 'bridge_halted') return {ok: true, json: async () => ({halted: true})};
      if (scenario === 'idle_then_stop' && seen.length === 1) {
        return {ok: true, json: async () => ({idle: true})};
      }
      get('stop').onclick();
      return {ok: true, json: async () => ({idle: true})};
    }},
  });
  try {
    await import(`../chrome-extension/worker.mjs?reconnect-test=${scenario}`);
    get('pairing').files = [{size: 150, text: async () => JSON.stringify({
      url: 'http://127.0.0.1:54321', token: 'a'.repeat(43), account: 'test@example.com',
    })}];
    await get('pairing').onchange();
    get('sending').checked = true; // Even a changed checkbox cannot bypass the send guard.
    get('status').textContent = 'Detenido: original_tab_missing (dispatch).';
    await get('start').onclick();
    if (scenario === 'worker_original') {
      assert.equal(replies.length, 1);
      assert.equal(replies[0].result?.complete, true, JSON.stringify(replies[0]));
      assert.equal(replies[0].result.emails[0].id, 'abcdef12');
      assert.deepEqual(closedTabs, [77]);
      assert.deepEqual(actions, ['profile', 'search', 'open', 'original', 'visibleOriginal', 'back']);
      return;
    }
    if (mismatch) {
      assert.equal(seen.length, 1);
      assert.match(get('version').textContent, /Solo lectura/);
      assert.equal(replies.length, 1);
      if (scenario.endsWith('_send')) {
        assert.equal(replies[0].error, 'extension_reload_required');
        assert.deepEqual(actions, []);
      } else {
        assert.deepEqual(replies[0].result, {email: 'test@example.com'});
        assert.deepEqual(actions, ['profile', 'profile']);
      }
      assert.equal(get('sending').disabled, true);
      return;
    }
    assert.match(seen[0], /^Conectando/);
    assert.doesNotMatch(get('status').textContent, /original_tab_missing/);
    assert.equal(get('version').textContent, `Código ${WORKER_VERSION} · Extensión instalada ${WORKER_VERSION}`);
    if (scenario === 'idle_then_stop') assert.match(seen[1], /^Conectado al puente/);
    assert.match(get('status').textContent,
      scenario === 'transport_failure' ? /browser_operation_failed$/ :
      scenario === 'bridge_halted' ? /bridge_needs_review$/ : /^Detenido por el usuario/);
    assert.equal(get('start').disabled, false);
    assert.equal(get('stop').disabled, true);
  } finally {
    for (const [key, descriptor] of saved) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  }
});
}

test('original-tab diagnostics distinguish rejection without weakening selection', () => {
  const source = {id: 1, url: 'https://mail.google.com/mail/u/0/#inbox'};
  const message = {domId: '#msg-f:123'};
  const tab = {id: 2, url: 'https://mail.google.com/mail/u/0/?view=om&permmsgid=msg-f:123'};
  for (const [candidate, existing, expected] of [
    [{...tab, openerTabId: 99}, new Set([1]), 'original_tab_opener_mismatch'],
    [tab, new Set([1, 2]), 'original_tab_reused'],
    [{...tab, url: tab.url.replace('/u/0/', '/u/1/')}, new Set([1]), 'original_tab_missing'],
    [{id: 2}, new Set([1]), 'original_tab_missing'],
  ]) {
    assert.deepEqual(originalTabs([candidate], existing, source, message), []);
    assert.equal(originalTabFailure([candidate], existing, source, message), expected);
  }
});

test('worker build and extension manifest versions are synchronized', () => {
  const manifest = JSON.parse(readFileSync(new URL('../chrome-extension/manifest.json', import.meta.url)));
  assert.equal(WORKER_VERSION, manifest.version);
});
