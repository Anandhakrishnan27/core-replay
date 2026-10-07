"""Discovery observation helpers: parse Playwright's ai-mode aria snapshot and capture element facts.

`page.aria_snapshot(mode="ai")` (Playwright 1.63, pinned) returns one YAML-like tree for the page and
every frame, with refs that carry a frame prefix (`f3e26`) and resolve via `page.locator("aria-ref=f3e26")`.

Everything here is pure (parsing, rendering) or an in-page script. Browser calls stay in Surface.
What leaves this module for the LLM or the trace is already redacted:
    typed values  → «redacted:len=n»
    masked text   → «masked»          (tenant mask_selectors)
    data values   → «shape:currency»  (see cua.safety.redact.page_value)
    other numbers → digit runs hashed (redact_digit_runs)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from cua.safety.redact import MASKED, redact_digit_runs, redact_typed

# Roles whose trailing text is a value someone typed or chose.
INPUT_ROLES = {"textbox", "searchbox", "combobox", "spinbutton"}
INTERACTIVE_ROLES = INPUT_ROLES | {
    "link",
    "button",
    "checkbox",
    "radio",
    "listbox",
    "option",
    "menuitem",
    "tab",
    "switch",
    "slider",
}
# Layout containers: never a useful ref on their own. Their accessible name can aggregate child text
# (e.g. a whole table row), so it is never shown to the LLM.
CONTAINER_ROLES = {"iframe", "table", "rowgroup", "row", "list", "group", "document", "none"}

_LINE_RE = re.compile(
    r'^(?P<indent>\s*- )(?P<role>[a-z]+)(?: "(?P<name>(?:[^"\\]|\\.)*)")?'
    r"(?P<attrs>(?: \[[^\]]*\])*)(?P<colon>:)?(?: (?P<text>.*))?$"
)
_URL_LINE_RE = re.compile(r"^(?P<indent>\s*- /url: )(?P<url>.*)$")
_REF_RE = re.compile(r"\[ref=([^\]]+)\]")
_NOISE_ATTRS_RE = re.compile(r" \[cursor=[^\]]*\]")


@dataclass
class AriaLine:
    indent: str
    role: str
    name: str | None
    attrs: str
    colon: bool
    text: str | None

    @property
    def ref(self) -> str | None:
        m = _REF_RE.search(self.attrs)
        return m.group(1) if m else None

    @property
    def wants_snapshot(self) -> bool:
        """Interactive elements, and anything else that shows text (cells, paragraphs, headings)."""
        if self.ref is None or self.role == "iframe":
            return False
        if self.role in INTERACTIVE_ROLES:
            return True
        return self.role not in CONTAINER_ROLES and bool(self.name or self.text)


def _unquote(name: str) -> str:
    try:
        return str(json.loads(f'"{name}"'))
    except json.JSONDecodeError:
        return name


def parse_line(line: str) -> AriaLine | None:
    m = _LINE_RE.match(line)
    if m is None:
        return None
    name = m.group("name")
    text = m.group("text")
    if text and len(text) >= 2 and text[0] == text[-1] == '"':
        text = _unquote(text[1:-1])  # YAML-quoted scalar, e.g. a typed number: "10002"
    return AriaLine(
        indent=m.group("indent"),
        role=m.group("role"),
        name=_unquote(name) if name is not None else None,
        attrs=m.group("attrs") or "",
        colon=m.group("colon") is not None,
        text=text,
    )


def render_line(line: AriaLine, *, name: str | None, text: str | None) -> str:
    out = f"{line.indent}{line.role}"
    if name:
        out += " " + json.dumps(name, ensure_ascii=False)
    out += _NOISE_ATTRS_RE.sub("", line.attrs)
    if line.colon:
        out += ":"
    if text:
        out += f" {text}"
    return out


def redact_snapshot(raw: str, safe_names: dict[str, str], frame_names: dict[str, str]) -> str:
    """The LLM-facing tree. `safe_names` maps ref → already-safe text (from the element snapshots).

    Typed values are reduced to their length; container names are dropped; a text-bearing element with
    no snapshot (detached mid-observation) shows «masked», because its mask status is unknown. Other
    lines get their digit runs hashed and URLs lose their query string.
    """
    out: list[str] = []
    for raw_line in raw.splitlines():
        url = _URL_LINE_RE.match(raw_line)
        if url:
            out.append(url.group("indent") + redact_digit_runs(urlsplit(url.group("url")).path))
            continue
        line = parse_line(raw_line)
        if line is None:
            out.append(redact_digit_runs(raw_line))
            continue
        ref = line.ref
        if line.role == "iframe" and ref in frame_names:
            out.append(render_line(line, name=frame_names[ref], text=None))
        elif line.role in INPUT_ROLES:
            name = safe_names.get(ref or "", redact_digit_runs(line.name or ""))
            typed = redact_typed(line.text) if line.text else None
            out.append(render_line(line, name=name, text=typed))
        elif line.role in CONTAINER_ROLES:
            out.append(render_line(line, name=None, text=None))
        elif ref in safe_names:
            # Name and trailing text are the element's own text: show its safe form once.
            safe = safe_names[ref]
            if line.name:
                out.append(render_line(line, name=safe, text=None))
            else:
                out.append(render_line(line, name=None, text=safe))
        elif line.wants_snapshot:
            out.append(
                render_line(line, name=MASKED if line.name else None, text=MASKED if line.text else None)
            )
        else:
            out.append(
                render_line(
                    line,
                    name=redact_digit_runs(line.name) if line.name else None,
                    text=redact_digit_runs(line.text) if line.text else None,
                )
            )
    return "\n".join(out)


# ---- in-page scripts --------------------------------------------------------------------------- #

# Facts about ONE element, evaluated in its own frame. Never returns a form control's value.
# `masked`: the element sits inside a tenant mask_selector. Row/caption cells carry their own flag.
ELEMENT_JS = r"""
(el, maskSelectors) => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const masked = (e) => maskSelectors.some((s) => { try { return !!e.closest(s); } catch (_) { return false; } });
  const cellInfo = (c) => ({ text: norm(c.innerText), masked: masked(c) });
  const tag = el.tagName.toLowerCase();
  const isControl = ['input', 'textarea', 'select'].includes(tag);
  const cell = el.closest('td, th');
  const row = cell ? cell.parentElement : null;

  let left = null;
  if (cell) {
    let c = cell.previousElementSibling;
    while (c && !norm(c.innerText)) c = c.previousElementSibling;
    if (c) left = cellInfo(c);
  }
  let table = null;
  if (cell && row) {
    const rows = Array.from(cell.closest('table').rows);
    const hdr = rows.find((r) => r.cells.length && Array.from(r.cells).every((c) => c.tagName === 'TH'));
    if (hdr && hdr !== row) {
      const headers = Array.from(hdr.cells).map((c) => norm(c.innerText));
      table = { headers, column: headers[cell.cellIndex] || '', row: Array.from(row.cells).map(cellInfo) };
    }
  }

  const q = (v) => v.replace(/\\/g, '\\\\').replace(/'/g, "\\'");
  const form = el.closest('form');
  const formName = form && form.getAttribute('name');
  const scope = formName ? `form[name='${q(formName)}'] ` : '';
  const nameAttr = el.getAttribute('name');
  const type = (el.getAttribute('type') || '').toLowerCase();
  let css = null;
  if (nameAttr) css = `${scope}${tag}[name='${q(nameAttr)}']`;
  else if (tag === 'input' && ['submit', 'button', 'reset'].includes(type)) css = `${scope}input[type='${type}']`;
  else if (tag === 'a' && el.getAttribute('href')) {
    const seg = el.getAttribute('href').split(/[?#]/)[0].split('/').filter(Boolean).pop();
    if (seg) css = `a[href*='${q(seg)}']`;
  }
  const cssParts = [], xpParts = [];
  for (let e = el; e && e.parentElement; e = e.parentElement) {
    const same = Array.from(e.parentElement.children).filter((s) => s.tagName === e.tagName);
    const i = same.indexOf(e) + 1;
    cssParts.unshift(`${e.tagName.toLowerCase()}:nth-of-type(${i})`);
    xpParts.unshift(`${e.tagName.toLowerCase()}[${i}]`);
  }
  if (!css) css = cssParts.join(' > ');

  let label = el.getAttribute('aria-label');
  if (!label && el.labels && el.labels.length) label = norm(el.labels[0].innerText);

  return {
    tag,
    name_attr: nameAttr,
    label: label || null,
    text: isControl ? null : norm(el.innerText ?? el.textContent).slice(0, 200),
    masked: masked(el),
    left,
    table,
    css,
    xpath: '/html/' + xpParts.join('/'),
  };
}
"""

# Frame/iframe element → its name (used to annotate `iframe` lines for the LLM).
FRAME_NAME_JS = "e => e.getAttribute('name') || ''"

# Headings and short UI strings in one frame. Data cells are excluded by construction:
#   heading  = h1-h6 / role=heading, or bold text that is alone in its table row (legacy title bars)
#   UI text  = links, buttons, th, labels, bold captions, and text outside tables
# Masked elements and texts with long numbers are dropped (Python filters numbers again).
PAGE_TEXT_JS = r"""
(maskSelectors) => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const masked = (e) => maskSelectors.some((s) => { try { return !!e.closest(s); } catch (_) { return false; } });
  const visible = (e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const ownText = (e) => norm(Array.from(e.childNodes).filter((n) => n.nodeType === 3).map((n) => n.textContent).join(' '));
  const headings = [], texts = [];
  if (!document.body) return { headings, texts };
  for (const e of document.body.querySelectorAll('*')) {
    if (['SCRIPT', 'STYLE', 'NOSCRIPT'].includes(e.tagName) || masked(e) || !visible(e)) continue;
    const type = (e.getAttribute('type') || '').toLowerCase();
    if (e.tagName === 'INPUT' && ['submit', 'button', 'reset'].includes(type)) { texts.push(norm(e.value)); continue; }
    if (['A', 'BUTTON', 'TH', 'LABEL'].includes(e.tagName)) { texts.push(norm(e.innerText)); continue; }
    if (e.closest('a, button, th, label') || !ownText(e)) continue;
    const text = norm(e.innerText);
    const cell = e.closest('td');
    const bold = (parseInt(getComputedStyle(e).fontWeight, 10) || 400) >= 600;
    const alone = cell && Array.from(cell.parentElement.cells).filter((c) => norm(c.innerText) || c.querySelector('input, select, textarea, button')).length === 1;
    if (/^H[1-6]$/.test(e.tagName) || e.getAttribute('role') === 'heading' || (bold && (!cell || alone))) {
      headings.push(text);
    } else if (!cell || bold) {
      texts.push(text);  // outside tables, or a bold caption cell
    }
  }
  return { headings, texts };
}
"""
