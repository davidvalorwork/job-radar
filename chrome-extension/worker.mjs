import {gmailDOM} from './gmail-dom.mjs';
import {originalTabs, originalTabFailure} from './original-tabs.mjs';
import {WORKER_VERSION, checkRequest, errorReport, pairingConfig, sendArguments, unwrapDOM} from './protocol.mjs';

const element = id => document.getElementById(id);
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const version = WORKER_VERSION;
const installedVersion = chrome.runtime.getManifest().version;
const diagnosticOnly = installedVersion !== version;
element('version').textContent = `Código ${version} · Extensión instalada ${installedVersion}` +
  (diagnosticOnly ? ' · Solo lectura: recarga la extensión antes de crear o enviar.' : '');
if (diagnosticOnly) {
  element('sending').checked = false;
  element('sending').disabled = true;
}
let config;
let running = false;
let selectedTab;
let current;
const originals = new Map();
let cacheBytes = 0;

async function tabs() {
  const choices = await chrome.tabs.query({url: 'https://mail.google.com/mail/*'});
  element('gmail').replaceChildren();
  for (const tab of choices.filter(tab => !new URL(tab.url).searchParams.has('view'))) {
    const option = document.createElement('option');
    option.value = tab.id;
    option.textContent = tab.title;
    element('gmail').append(option);
  }
}

async function rpc(path, data = {}) {
  const response = await fetch(config.url + path, {
    method: 'POST', headers: {'Content-Type': 'application/json', Authorization: 'Bearer ' + config.token},
    body: JSON.stringify(data), signal: AbortSignal.timeout(10000), cache: 'no-store',
  });
  if (!response.ok) throw new Error('local_bridge_unavailable');
  return response.json();
}

async function dom(action, args = {}, tabId = selectedTab) {
  checkRequest(current);
  const result = await chrome.scripting.executeScript({
    target: {tabId}, world: 'ISOLATED', func: gmailDOM,
    args: [action, {...args, account: config.account, deadline: current.expires_at * 1000, errorEnvelope: true}],
  });
  return unwrapDOM(result, action === 'saveDraft' ? 'draft' : action);
}

async function readOriginal(message, labels) {
  const cached = originals.get(message.id);
  if (cached) return {...cached, labels};
  const existing = new Set((await chrome.tabs.query({url: 'https://mail.google.com/*'})).map(t => t.id));
  const source = {...await chrome.tabs.get(selectedTab),
    workerId: (await chrome.tabs.getCurrent())?.id};
  await dom('original', message);
  let originalTab;
  let observedTabs = [];
  try {
    const deadline = Math.min(current.expires_at * 1000, Date.now() + 15000);
    while (Date.now() < deadline) {
      observedTabs = await chrome.tabs.query({url: 'https://mail.google.com/*'});
      const found = originalTabs(observedTabs, existing, source, message);
      if (found.length > 1) throw new Error('ambiguous_original_tab');
      if (found.length === 1) {
        originalTab = found[0];
        if (originalTab.status === 'complete') break;
      }
      await sleep(200);
    }
    if (!originalTab) throw new Error(originalTabFailure(observedTabs, existing, source, message));
    if (originalTab.status !== 'complete') throw new Error('ui_timeout');
    // Gmail renders the complete provider MIME visibly in the validated Show
    // original tab. Read that element directly; no separate download or debugger.
    const {raw} = await dom('visibleOriginal', {}, originalTab.id);
    const record = {id: message.id, raw, labels};
    while (cacheBytes + raw.length > 16 * 1024 * 1024 && originals.size) {
      const key = originals.keys().next().value;
      cacheBytes -= originals.get(key).raw.length;
      originals.delete(key);
    }
    originals.set(message.id, record);
    cacheBytes += raw.length;
    return {...record, labels};
  } catch (error) {
    error.phase = error.phase || 'read_original';
    throw error;
  } finally {
    if (originalTab) await chrome.tabs.remove(originalTab.id).catch(() => {});
  }
}

async function search(query) {
  if (typeof query !== 'string' || query.length > 1000) throw new Error('invalid_query');
  const sent = query.startsWith('in:sent ');
  const drafts = query.startsWith('in:drafts ');
  if (!sent && !drafts && !query.startsWith('in:anywhere ')) throw new Error('unsupported_query');
  // Contact suppression only needs non-SENT messages. Do not infer Gmail labels from From.
  let page = await dom('search', {query: sent ? query : query + ' -in:sent'});
  const records = [];
  const seen = new Set();
  const pages = new Set();
  for (let index = 0; index < 20; index++) {
    // Detect unsupported grouping before downloading any originals on this page.
    if (page.rows.some(row => !row.draft && row.messageId !== row.id)) {
      throw new Error('disable_conversation_view_then_retry');
    }
    const signature = page.rows.map(row => row.id).join(',');
    if (pages.has(signature)) throw new Error('repeated_search_page');
    pages.add(signature);
    for (const row of page.rows) {
      checkRequest(current);
      if (row.draft) {
        records.push({id: row.id, labels: ['DRAFT']});
        continue;
      }
      if (seen.has(row.id)) continue;
      seen.add(row.id);
      if (originals.has(row.id)) {
        const record = {...originals.get(row.id), labels: sent ? ['SENT'] : []};
        originals.set(row.id, record);
        records.push(record);
      } else {
        const messages = await dom('open', {id: row.id});
        if (messages.length !== 1 || messages[0].id !== row.id) throw new Error('incomplete_conversation');
        records.push(await readOriginal(messages[0], sent ? ['SENT'] : []));
        await dom('back');
      }
      if (JSON.stringify(records).length > 20 * 1024 * 1024) throw new Error('history_too_large');
    }
    if (!page.next) return {emails: records, complete: true};
    page = await dom('next');
  }
  throw new Error('incomplete_history');
}

async function execute(request, enabled) {
  current = request;
  checkRequest(current);
  if (diagnosticOnly && ['gmail_send_email', 'gmail_create_draft'].includes(request.method)) {
    throw new Error('extension_reload_required');
  }
  await dom('profile');
  if (request.method === 'gmail_get_profile') return dom('profile');
  if (request.method === 'gmail_search_emails') return search(request.arguments.query);
  if (request.method === 'gmail_read_email') {
    const cached = originals.get(request.arguments.message_id);
    if (!cached) throw new Error('message_not_observed_this_session');
    if (!cached.labels.includes('SENT')) return cached;
    // Cached body is immutable; SENT is established by a fresh native Gmail search.
    const found = await search('in:sent rfc822msgid:' +
      cached.raw.match(/^message-id:\s*(.+)$/im)[1].trim());
    const record = found.emails.find(item => item.id === request.arguments.message_id);
    if (!record) throw new Error('sent_message_not_found');
    return record;
  }
  if (request.method === 'gmail_create_draft') {
    const mail = sendArguments(request.arguments);
    if (mail.from.toLowerCase() !== config.account) throw new Error('account_mismatch');
    const existing = await search('in:drafts to:' + mail.to);
    if (existing.emails.length) throw new Error('existing_draft_needs_review');
    await dom('compose', mail);
    checkRequest(current);
    await dom('saveDraft', mail);
    const saved = await search('in:drafts to:' + mail.to);
    if (saved.emails.length !== 1 || !saved.emails[0].id) throw new Error('draft_not_saved');
    return {id: saved.emails[0].id};
  }
  if (!enabled || !element('sending').checked) throw new Error('sending_disabled');
  const mail = sendArguments(request.arguments);
  if (mail.from.toLowerCase() !== config.account) throw new Error('account_mismatch');
  await dom('compose', mail);
  checkRequest(current);
  const message = await dom('send', {...mail, allowSend: true});
  // Read actual provider MIME now; a subsequent independent search verifies SENT.
  await readOriginal(message, ['SENT']);
  return {id: message.id};
}

element('refresh').onclick = tabs;
element('pairing').onchange = async () => {
  try {
    const file = element('pairing').files[0];
    if (!file || file.size > 4096) throw new Error('invalid_pairing');
    config = pairingConfig(JSON.parse(await file.text()));
    element('start').disabled = false;
    element('status').textContent = 'Conexión cargada. Cuenta: ' + config.account;
  } catch {element('status').textContent = 'Archivo de conexión inválido.';}
};
element('stop').onclick = () => {
  running = false;
  element('status').textContent = 'Deteniendo después de la operación actual…';
};
element('start').onclick = async () => {
  if (running || !config || !element('gmail').value) return;
  // Only one extension worker even if the toolbar is clicked twice.
  await navigator.locks.request('job-radar-gmail-worker', {ifAvailable: true}, async lock => {
    if (!lock) {element('status').textContent = 'Ya existe otro trabajador.'; return;}
    selectedTab = Number(element('gmail').value);
    running = true;
    for (const id of ['start', 'pairing', 'gmail', 'refresh', 'sending']) element(id).disabled = true;
    element('stop').disabled = false;
    let completed = 0;
    let failed = false;
    element('status').textContent = 'Conectando con el puente local…';
    try {
      while (running) {
        const command = await rpc('/next', {worker_version: version});
        if (command.halted || command.busy) throw new Error('bridge_needs_review');
        if (!command.request) {
          if (!running) break;
          element('status').textContent = `Conectado al puente.${diagnosticOnly ? ' Solo lectura.' : ''} Esperando solicitudes. Operaciones: ${completed}. Llamadas a IA: 0.`;
          await sleep(500);
          continue;
        }
        if (command.account !== config.account) throw new Error('account_mismatch');
        element('status').textContent = 'Procesando solicitud. Llamadas a IA: 0.';
        let response;
        try {
          const result = await execute(command.request, command.enable_send);
          response = {id: command.request.id, result};
        } catch (error) {
          const detail = errorReport(error);
          response = {id: command.request.id, ...detail};
          running = false;
          failed = true;
          element('status').textContent = `Detenido: ${detail.error} (${detail.phase}). No reintentar un envío incierto.`;
        }
        // Exactly one response attempt. Lost acknowledgement stops, never resends.
        const receipt = await rpc('/result', response);
        if (receipt.halted) {
          running = false;
          failed = true;
          const detail = errorReport({message: receipt.error_code, phase: receipt.phase});
          element('status').textContent = `Detenido: ${detail.error} (${detail.phase}). No reintentar un envío incierto.`;
        }
        if (running) element('status').textContent = `Operaciones: ${++completed}. Llamadas a IA: 0.`;
      }
    } catch (error) {
      failed = true;
      element('status').textContent = 'Detenido: ' + errorReport(error).error;
    }
    finally {
      running = false;
      originals.clear();
      cacheBytes = 0;
      for (const id of ['start', 'pairing', 'gmail', 'refresh', 'sending']) element(id).disabled = false;
      element('sending').disabled = diagnosticOnly;
      element('stop').disabled = true;
      if (!failed) element('status').textContent = `Detenido por el usuario. Operaciones: ${completed}. No se reintentó ningún envío.`;
    }
  });
};
await tabs();
