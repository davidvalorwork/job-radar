// Correlate the tab opened by Gmail's Show original action with the requested message.
export function originalTabs(tabs, existing, source, message) {
  const parent = new URL(source.url);
  if (parent.origin !== 'https://mail.google.com' || !/^\/mail\/u\/\d+\/?$/.test(parent.pathname)) {
    throw new Error('invalid_original_link');
  }
  return tabs.filter(tab => {
    if (!Number.isInteger(tab.id) || existing.has(tab.id) || tab.id === source.id) return false;
    try {
      const url = new URL(tab.url);
      // An absent opener is not evidence that the original failed to open.
      // Correlate only NEW tabs, same Gmail account path and exact provider message.
      // Chrome can attribute the open to the extension that injected the action.
      // Only the Gmail tab or this worker (getCurrent), never an arbitrary opener.
      return (tab.openerTabId == null || tab.openerTabId === source.id ||
        (Number.isInteger(source.workerId) && tab.openerTabId === source.workerId)) &&
        url.origin === parent.origin &&
        url.pathname.replace(/\/$/, '') === parent.pathname.replace(/\/$/, '') &&
        url.searchParams.get('view') === 'om' &&
        url.searchParams.get('permmsgid') === message.domId.replace(/^#/, '');
    } catch {return false;}
  });
}

// Diagnostics only: these relaxed comparisons NEVER select, read or close a tab.
export function originalTabFailure(tabs, existing, source, message) {
  const withoutOpener = tabs.map(tab => ({...tab, openerTabId: undefined}));
  if (originalTabs(withoutOpener, existing, source, message).length) {
    return 'original_tab_opener_mismatch';
  }
  if (originalTabs(withoutOpener, new Set(), source, message).some(tab => existing.has(tab.id))) {
    return 'original_tab_reused';
  }
  return 'original_tab_missing';
}
