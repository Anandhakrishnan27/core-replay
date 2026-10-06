"""Custom Playwright selector engines for legacy markup: `table_cell` and `near_text`.

Registered once per Playwright instance, before any context exists (see cua.surface.browser).
The selector body is JSON (the schema locator minus `strategy`), e.g.
    table_cell={"table_has_header": "Account Type", "row_contains": "Share Savings", "column_header": "Balance"}
Both return real lazy Locators, so count(), auto-wait and screenshot masks work as usual.
"""

from __future__ import annotations

from typing import Any

# Header row = a row of the table's OWN rows (table.rows excludes nested tables) with a cell whose
# normalized text equals `table_has_header`. Column = index of `column_header` in that row.
# Rows = the table's other rows with a cell containing `row_contains`. No colspan support.
TABLE_CELL_JS = """
(() => {
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const queryAll = (root, body) => {
    const a = JSON.parse(body);
    const out = [];
    const tables = [];
    if (root.tagName === 'TABLE') tables.push(root);
    if (root.querySelectorAll) tables.push(...root.querySelectorAll('table'));
    for (const t of tables) {
      const rows = Array.from(t.rows);
      const hdr = rows.find((r) => Array.from(r.cells).some((c) => norm(c.textContent) === a.table_has_header));
      if (!hdr) continue;
      const col = Array.from(hdr.cells).findIndex((c) => norm(c.textContent) === a.column_header);
      if (col < 0) continue;
      for (const r of rows) {
        if (r === hdr) continue;
        if (!Array.from(r.cells).some((c) => norm(c.textContent).includes(a.row_contains))) continue;
        if (r.cells[col]) out.push(r.cells[col]);
      }
    }
    return out;
  };
  return { queryAll, query: (root, body) => queryAll(root, body)[0] || null };
})()
"""

# Anchor = smallest visible element whose normalized text equals `anchor_text`.
#   following_input: first control after the anchor in document order, within the anchor's <tr> if any
#   right_of / below: nearest control by bounding box that overlaps the anchor vertically / horizontally
# `role` filters candidates BEFORE picking the nearest.
NEAR_TEXT_JS = """
(() => {
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const CONTROLS = 'input:not([type=hidden]), textarea, select, button';
  const TEXT_TYPES = ['', 'text', 'search', 'email', 'tel', 'url'];
  const BUTTON_TYPES = ['submit', 'button', 'reset', 'image'];
  const type = (e) => (e.getAttribute('type') || '').toLowerCase();
  const ROLES = {
    textbox: (e) => e.tagName === 'TEXTAREA' || (e.tagName === 'INPUT' && TEXT_TYPES.includes(type(e))),
    combobox: (e) => e.tagName === 'SELECT' && !e.multiple,
    listbox: (e) => e.tagName === 'SELECT' && e.multiple,
    checkbox: (e) => e.tagName === 'INPUT' && type(e) === 'checkbox',
    radio: (e) => e.tagName === 'INPUT' && type(e) === 'radio',
    spinbutton: (e) => e.tagName === 'INPUT' && type(e) === 'number',
    button: (e) => e.tagName === 'BUTTON' || (e.tagName === 'INPUT' && BUTTON_TYPES.includes(type(e))),
  };
  const visible = (e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const anchorsIn = (root, text) => {
    const all = Array.from(root.querySelectorAll('*')).filter(
      (e) => !['SCRIPT', 'STYLE', 'HEAD', 'TITLE'].includes(e.tagName) && norm(e.textContent) === text && visible(e));
    return all.filter((e) => !all.some((o) => o !== e && e.contains(o)));
  };
  const nearest = (anchor, cands, rel) => {
    const a = anchor.getBoundingClientRect();
    let best = null, bestD = Infinity;
    for (const c of cands) {
      if (!visible(c)) continue;
      const r = c.getBoundingClientRect();
      let d = Infinity;
      if (rel === 'right_of' && r.left >= a.right - 1 && r.top < a.bottom && r.bottom > a.top) d = r.left - a.right;
      if (rel === 'below' && r.top >= a.bottom - 1 && r.left < a.right && r.right > a.left) d = r.top - a.bottom;
      if (d < bestD) { best = c; bestD = d; }
    }
    return best;
  };
  const queryAll = (root, body) => {
    const a = JSON.parse(body);
    const roleOk = a.role ? (ROLES[a.role] || (() => false)) : () => true;
    const scope = root.querySelectorAll ? root : document;
    const out = new Set();
    for (const anchor of anchorsIn(scope, a.anchor_text)) {
      let hit = null;
      if (a.relation === 'following_input') {
        const box = anchor.closest('tr') || scope;
        hit = Array.from(box.querySelectorAll(CONTROLS)).find(
          (c) => (anchor.compareDocumentPosition(c) & Node.DOCUMENT_POSITION_FOLLOWING) && roleOk(c)) || null;
      } else {
        hit = nearest(anchor, Array.from(scope.querySelectorAll(CONTROLS)).filter(roleOk), a.relation);
      }
      if (hit) out.add(hit);
    }
    return Array.from(out);
  };
  return { queryAll, query: (root, body) => queryAll(root, body)[0] || null };
})()
"""

ENGINES = {"table_cell": TABLE_CELL_JS, "near_text": NEAR_TEXT_JS}


async def register_engines(playwright: Any) -> None:
    """Register both engines on a Playwright instance. Must run before any browser context is created."""
    for name, script in ENGINES.items():
        await playwright.selectors.register(name, script=script)
