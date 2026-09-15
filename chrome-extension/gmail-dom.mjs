// Executed in Chrome's ISOLATED world. No Gmail internals, cookies, storage or APIs.
// Fail closed when semantic controls change. English and Spanish UI only.
export async function gmailDOM(action, args = {}) {
  try {
  const visible = e => !!e.getClientRects().length;
  const all = (selector, root = document) => Array.from(root.querySelectorAll(selector)).filter(visible);
  const one = elements => {
    if (elements.length !== 1) throw new Error("ambiguous_or_missing_control");
    return elements[0];
  };
  const text = e => (e.getAttribute("aria-label") || e.getAttribute("title") ||
    e.getAttribute("data-tooltip") || e.textContent).trim();
  const control = (names, root = document) => one(all('button,[role="button"],[role="menuitem"],a', root)
    .filter(e => names.includes(text(e))));
  const nextLabels = ["Older", "Next", "Next page", "Next results", "Siguiente", "Resultados siguientes"];
  const wait = async predicate => {
    const deadline = Math.min(args.deadline || Date.now() + 15000, Date.now() + 15000);
    while (Date.now() < deadline) {
      const result = predicate();
      if (result) return result;
      await new Promise(resolve => setTimeout(resolve, 150));
    }
    throw new Error("ui_timeout");
  };
  const fill = (element, value) => {
    if (element.isContentEditable) element.textContent = value;
    else {
      const proto = element.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, "value").set.call(element, value);
    }
    element.dispatchEvent(new InputEvent("input", {bubbles: true, inputType: "insertText", data: value}));
    element.dispatchEvent(new Event("change", {bubbles: true}));
  };
  const profile = () => {
    const values = all('a[aria-label]').map(e => e.getAttribute("aria-label"))
      .filter(s => /^(Cuenta de Google:|Google Account:)/.test(s));
    return one(values).match(/[\w.!#$%&'*+/=?^`{|}~-]+@[\w.-]+\.[a-z]{2,}/i)?.[0].toLowerCase();
  };
  const messages = () => all('[data-legacy-message-id]').map(e => ({
    id: e.getAttribute("data-legacy-message-id"), domId: e.getAttribute("data-message-id"),
  }));
  const snapshot = () => {
    const main = one(all('[role="main"]'));
    const rows = all('tr[role="row"]', main).map(row => {
      const span = row.querySelector('[data-legacy-thread-id]');
      return span ? {id: span.getAttribute('data-legacy-thread-id'),
        messageId: span.getAttribute('data-legacy-last-message-id'),
        draft: /\b(Draft|Borrador)\b/i.test(row.innerText)} : null;
    }).filter(Boolean);
    const empty = /No conversations found|No results found|No hay conversaciones|No se ha encontrado|No se encontraron/i.test(main.innerText);
    const next = all('[role="button"],button').filter(e => nextLabels.includes(text(e)));
    if (rows.length && next.length !== 1) throw new Error("unknown_pagination");
    if (!rows.length && !empty) throw new Error("search_not_ready");
    return {rows, next: next.length === 1 && next[0].getAttribute('aria-disabled') !== 'true' && !next[0].disabled};
  };
  const verifyCompose = () => {
    const dialog = one(all('[role="dialog"]').filter(e => e.querySelector('input[name="subjectbox"]')));
    const emails = all('[email],[data-hovercard-id]', dialog).map(e =>
      e.getAttribute('email') || e.getAttribute('data-hovercard-id')).filter(Boolean);
    if (!emails.length || emails.some(email => email.toLowerCase() !== args.to.toLowerCase())) {
      throw new Error("recipient_mismatch");
    }
    const body = one(all('[contenteditable="true"][role="textbox"]', dialog));
    if (one(all('input[name="subjectbox"]', dialog)).value !== args.subject ||
        body.innerText.replace(/\r\n/g, '\n').trimEnd() !== args.body.replace(/\r\n/g, '\n').trimEnd() ||
        all('[role="progressbar"]', dialog).length || !dialog.innerText.includes(args.filename)) {
      throw new Error("compose_not_ready");
    }
    // Gmail renders extra recipient inputs only when Cc/Bcc has been expanded.
    if (all('input[name="cc"],input[name="bcc"],textarea[name="cc"],textarea[name="bcc"]', dialog)
      .some(e => e.value)) throw new Error("unexpected_cc_bcc");
    return dialog;
  };
  if (action === "visibleOriginal") {
    const raw = one(all('pre#raw_message_text')).innerText;
    if (raw.length > 8 * 1024 * 1024) throw new Error("original_too_large");
    if (!/^message-id:/im.test(raw) || !/^date:/im.test(raw)) throw new Error("invalid_original");
    return {raw};
  }
  if (profile() !== args.account) throw new Error("account_mismatch");
  if (args.deadline && Date.now() >= args.deadline) throw new Error("expired_request");
  if (action === "profile") return {email: profile()};
  if (action === "search") {
    if (all('input[name="subjectbox"]').length) throw new Error("open_compose_needs_review");
    const input = one(all('input[name="q"]'));
    fill(input, args.query);
    input.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}));
    input.dispatchEvent(new KeyboardEvent('keyup', {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}));
    await wait(() => location.hash.startsWith('#search/') &&
      decodeURIComponent(location.hash.slice(8).split('/')[0].replace(/\+/g, ' ')) === args.query);
    // Gmail updates asynchronously; require the same result signature twice after settling.
    await new Promise(resolve => setTimeout(resolve, 800));
    let previous;
    let lastError;
    try {
      return await wait(() => {
        try {
          const result = snapshot();
          const signature = JSON.stringify(result);
          lastError = undefined;
          if (signature === previous) return result;
          previous = signature;
        } catch (error) {lastError = error; previous = undefined;}
        return null;
      });
    } catch (error) {throw lastError || error;}
  }
  if (action === "page") return snapshot();
  if (action === "next") {
    const previous = JSON.stringify(snapshot().rows);
    if (!snapshot().next) throw new Error("no_next_page");
    control(nextLabels).click();
    return await wait(() => {const value = snapshot(); return JSON.stringify(value.rows) !== previous && value;});
  }
  if (action === "open") {
    one(all('[data-legacy-thread-id]').filter(e => e.getAttribute('data-legacy-thread-id') === args.id)).click();
    await wait(() => messages().length);
    const expand = all('[role="button"],button').filter(e => ["Expand all", "Expandir todo", "Expandir todos"].includes(text(e)));
    if (expand.length === 1) {expand[0].click(); await new Promise(resolve => setTimeout(resolve, 300));}
    // Hidden/collapsed messages must not silently disappear from the history.
    const allMessages = document.querySelectorAll('[data-legacy-message-id]');
    const result = messages();
    if (result.length !== allMessages.length || all('[role="button"]').some(e => /mensajes más|more messages/i.test(text(e)))) {
      throw new Error("incomplete_conversation");
    }
    return result;
  }
  if (action === "original") {
    const message = one(all('[data-legacy-message-id]').filter(e => e.getAttribute('data-legacy-message-id') === args.id));
    control(["Más opciones de mensajes", "More message options", "More"], message).click();
    await wait(() => all('[role="menuitem"]').some(e => ["Mostrar original", "Show original"].includes(text(e))));
    control(["Mostrar original", "Show original"]).click();
    return true;
  }
  if (action === "back") {
    const backLabels = ["Volver a los resultados de búsqueda", "Volver a Resultados de la búsqueda",
      "Back to search results", "Volver", "Back"];
    // Closing Show original can make Gmail ignore the first synthetic click.
    // Back is reversible; retry the same exact visible control until the result
    // grid is observable, never treating the message view as a completed search.
    let retryAt = 0;
    return await wait(() => {
      try {return snapshot();} catch {}
      if (Date.now() >= retryAt) {
        control(backLabels).click();
        retryAt = Date.now() + 300;
      }
      return null;
    });
  }
  if (action === "compose") {
    if (all('input[name="subjectbox"]').length) throw new Error("open_compose_needs_review");
    control(["Redactar", "Compose"]).click();
    await wait(() => all('input[name="subjectbox"]').length === 1);
    const subject = one(all('input[name="subjectbox"]'));
    const dialog = subject.closest('[role="dialog"]');
    if (!dialog) throw new Error("missing_compose_dialog");
    const to = one(all('input[name="to"],textarea[name="to"],input[aria-label="Destinatarios"],input[aria-label="To recipients"]', dialog));
    fill(to, args.to);
    to.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true}));
    fill(subject, args.subject);
    fill(one(all('[contenteditable="true"][role="textbox"]', dialog)), args.body);
    const input = one(Array.from(dialog.querySelectorAll('input[type="file"]')));
    const binary = atob(args.base64.replace(/-/g, '+').replace(/_/g, '/'));
    if (!binary.startsWith('%PDF-')) throw new Error("invalid_pdf");
    const data = new DataTransfer();
    data.items.add(new File([Uint8Array.from(binary, char => char.charCodeAt(0))], args.filename, {type: 'application/pdf'}));
    input.files = data.files;
    input.dispatchEvent(new Event('change', {bubbles: true}));
    await wait(() => {try {return !!verifyCompose();} catch {return false;}});
    return {ready: true};
  }
  if (action === "saveDraft") {
    const dialog = verifyCompose();
    // This precise Gmail control both persists the already verified message and
    // closes the compose window. It is intentionally not a generic close click.
    control(["Guardar y cerrar", "Save & close"], dialog).click();
    await wait(() => !visible(dialog));
    return {saved: true};
  }
  if (action === "send") {
    const dialog = verifyCompose();
    // Last possible guard, immediately before the irreversible click.
    if (Date.now() >= args.deadline || args.allowSend !== true) throw new Error("send_not_authorized");
    one(all('[role="button"],button', dialog).filter(e => /^(Enviar|Send)(\s|$|\()/.test(text(e)) &&
      !/program|schedul/i.test(text(e)))).click();
    await wait(() => !visible(dialog));
    const notice = await wait(() => all('[role="alert"],[role="status"]').find(e => /Mensaje enviado|Message sent/i.test(e.textContent)));
    one(all('a,[role="link"],button,[role="button"]', notice).filter(e => ["Ver mensaje", "View message"].includes(text(e)))).click();
    const found = await wait(() => messages().length && messages());
    // Do not guess which message was sent inside a grouped conversation.
    if (found.length !== 1) throw new Error("sent_conversation_needs_review");
    return {id: found[0].id, domId: found[0].domId};
  }
  throw new Error("unsupported_action");
  } catch (error) {
    // Chrome can omit rejected executeScript results. Return a structured error
    // inside the isolated script; the worker validates its allowlisted code.
    if (args.errorEnvelope === true) {
      return {radarError: {code: error instanceof Error ? error.message : "browser_script_failed", phase: action}};
    }
    throw error;
  }
}
