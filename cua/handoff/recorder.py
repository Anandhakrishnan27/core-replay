"""Capture what the human does in the live session (redacted). Phase 5.

Mechanism: page.expose_binding("__cuaRecord", handler) + add_init_script(LISTENER_JS) on the SAME context.
The listener reports click/change/submit with a semantic hint (role, accessible name, label);
typed values are reduced to their length before leaving the page.
"""

from __future__ import annotations

LISTENER_JS = r"""
(() => {
  if (window.__cuaListening) return; window.__cuaListening = true;
  const hint = (el) => {
    const role = el.getAttribute('role') || el.tagName.toLowerCase();
    const name = (el.getAttribute('aria-label') || el.innerText || el.value || el.name || '').trim().slice(0, 60);
    return `${role} "${name}"`;
  };
  document.addEventListener('click', e => window.__cuaRecord?.({kind: 'click', target: hint(e.target)}), true);
  document.addEventListener('change', e => {
    const v = (e.target.value || '');
    window.__cuaRecord?.({kind: 'fill', target: hint(e.target), value: `«redacted:len=${v.length}»`});
  }, true);
})();
"""
