export const ERROR_CODES = new Set([
  'unknown_pagination', 'search_not_ready', 'ui_timeout', 'no_next_page',
  'ambiguous_or_missing_control', 'account_mismatch', 'expired_request',
  'open_compose_needs_review', 'incomplete_conversation', 'disable_conversation_view_then_retry',
  'repeated_search_page', 'incomplete_history', 'history_too_large',
  'browser_script_failed', 'browser_operation_failed', 'local_bridge_unavailable',
  'bridge_needs_review', 'invalid_original_link', 'invalid_original', 'original_tab_missing',
  'original_tab_opener_mismatch', 'original_tab_reused',
  'extension_reload_required',
  'ambiguous_original_tab', 'original_too_large',
  'invalid_pairing', 'invalid_or_expired_request', 'invalid_query', 'unsupported_query',
  'invalid_send_payload', 'message_not_observed_this_session', 'sent_message_not_found',
  'sending_disabled', 'send_not_authorized', 'invalid_pdf', 'missing_compose_dialog',
  'recipient_mismatch', 'compose_not_ready', 'unexpected_cc_bcc',
  'sent_conversation_needs_review', 'draft_not_saved', 'unsupported_action',
]);
const PHASES = new Set(['profile', 'search', 'page', 'next', 'open', 'original',
  'back', 'compose', 'draft', 'send', 'read_original', 'dispatch']);

export function errorReport(error) {
  return {error: ERROR_CODES.has(error?.message) ? error.message : 'browser_operation_failed',
    phase: PHASES.has(error?.phase) ? error.phase : 'dispatch'};
}

export function unwrapDOM(results, phase) {
  const envelope = results?.[0]?.result;
  const code = results?.length !== 1 || results[0].error || envelope === undefined
    ? 'browser_script_failed' : envelope?.radarError
      ? envelope.radarError.code || 'browser_script_failed' : undefined;
  if (code) {
    const error = new Error(ERROR_CODES.has(code) ? code : 'browser_script_failed');
    error.phase = PHASES.has(phase) ? phase : 'dispatch';
    throw error;
  }
  return envelope;
}

export function pairingConfig(value) {
  const url = new URL(value.url);
  if (url.protocol !== "http:" || url.hostname !== "127.0.0.1" || !url.port ||
      url.username || url.password || url.search || url.hash || url.pathname !== "/" ||
      !/^[\w-]{40,80}$/.test(value.token) || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value.account)) {
    throw new Error("invalid_pairing");
  }
  return {url: url.origin, token: value.token, account: value.account.toLowerCase()};
}

export function checkRequest(request, now = Date.now()) {
  if (!/^[a-f0-9]{32}$/.test(request.id) || !Number.isFinite(request.expires_at) ||
      request.expires_at * 1000 <= now || !request.arguments ||
      !["gmail_get_profile", "gmail_search_emails", "gmail_read_email", "gmail_send_email", "gmail_create_draft"].includes(request.method)) {
    throw new Error("invalid_or_expired_request");
  }
}

export function sendArguments(args) {
  const parts = args.payload?.parts;
  if (!/^[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+$/.test(args.to) ||
      !/^[^\r\n]{1,200}$/.test(args.subject) || parts?.length !== 2 ||
      parts[0].mime_type !== "text/plain" || typeof parts[0].body?.content !== "string" ||
      parts[0].body.content.length > 20000 || parts[1].mime_type !== "application/pdf" ||
      !/^[^/\\\r\n]{1,100}\.pdf$/i.test(parts[1].filename) ||
      !/^[A-Za-z0-9_+\/-]+={0,2}$/.test(parts[1].body?.base64_url_content || "") ||
      parts[1].body.base64_url_content.length > 8 * 1024 * 1024) {
    throw new Error("invalid_send_payload");
  }
  return {to: args.to, from: args.from_address, subject: args.subject,
    body: parts[0].body.content, filename: parts[1].filename,
    base64: parts[1].body.base64_url_content};
}
export const WORKER_VERSION = '0.1.8';
