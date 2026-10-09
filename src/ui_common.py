"""Formatting and form-parsing helpers shared by the full node's web app
and the light client. Light-safe: imports nothing that pulls in the VDF,
the chain database, or the node, and a test keeps it that way."""

import logging
import os
import re
import sys

from flask import Flask, send_file
from markupsafe import Markup, escape

import crypto as crypto_mod
import tx as tx_mod
from params import TICKS_PER_LAPSE


def fmt_balance(ticks):
    lapse = ticks // TICKS_PER_LAPSE
    rem = ticks % TICKS_PER_LAPSE
    return f"{lapse} LAPSE {rem:,} ticks"


def fmt_lapse(ticks):
    """Whole-LAPSE amount only, comma-grouped, for compact display."""
    return f"{ticks // TICKS_PER_LAPSE:,} LAPSE"


def fmt_lapse_dp(ticks, places=4):
    """LAPSE with decimals: "28,708.2599 LAPSE".

    For headline figures, where "28,708 LAPSE 25,987,856 ticks" is nine
    digits of tick that wrap the line and answer nothing anyone asked. The
    exact tick count is still on /api/info for anything that needs it.
    """
    return f"{ticks / TICKS_PER_LAPSE:,.{places}f} LAPSE"


def fmt_duration(seconds):
    """A span as the two largest units that fit: "1y 24d", "3d 4h", "12m".

    Two units, never more: the point is a glanceable age, and seconds of
    precision on something measured in days is noise dressed as detail.
    """
    seconds = max(int(seconds or 0), 0)
    units = (("y", 31_536_000), ("d", 86_400), ("h", 3_600), ("m", 60))
    parts = []
    for suffix, size in units:
        if seconds >= size or parts:
            count, seconds = divmod(seconds, size)
            if count or parts:
                parts.append(f"{count}{suffix}")
            if len(parts) == 2:
                return " ".join(parts)
    return " ".join(parts) if parts else "just now"


def _pagination_window(page, total_pages, radius=2):
    """Page numbers to render as links: always the first and last page,
    the current page and `radius` neighbors on each side, and None where
    a gap between those is skipped (rendered as an ellipsis)."""
    if total_pages <= 1:
        return [1]
    keep = {1, total_pages}
    for p in range(page - radius, page + radius + 1):
        if 1 <= p <= total_pages:
            keep.add(p)
    window = []
    prev = None
    for p in sorted(keep):
        if prev is not None and p - prev > 1:
            window.append(None)
        window.append(p)
        prev = p
    return window


# A tiny, safe markdown-like subset for board post text, which is public
# and written by anyone: real markdown.markdown() (used for the shipped
# whitepaper.md elsewhere in this file) passes raw HTML straight through
# unless separately sanitized, and this text is the one place on the site
# that is untrusted, attacker-controlled input rendered to other people's
# browsers. Escaping happens first, so every substitution below only ever
# wraps already-escaped text in tags it introduces itself; by the time any
# pattern runs, there is no way for a post's own content to contain a
# literal '<', so nothing typed into it can inject an element of its own.
_BOARD_CODE_RE = re.compile(r'`([^`]+?)`')


_BOARD_BOLD_RE = re.compile(r'\*\*(.+?)\*\*')


_BOARD_ITALIC_RE = re.compile(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)')


_BOARD_LINK_RE = re.compile(r'\[([^\]\n]+?)\]\((https?://[^\s()<>]+)\)')


_BOARD_URL_RE = re.compile(r'(https?://[^\s<]+)')


_BOARD_QUOTE_LINE_RE = re.compile(r'^&gt; ?(.*)$')


def render_board_text(raw):
    text = str(escape(raw))
    text = _BOARD_CODE_RE.sub(r'<code>\1</code>', text)
    text = _BOARD_BOLD_RE.sub(r'<strong>\1</strong>', text)
    text = _BOARD_ITALIC_RE.sub(r'<em>\1</em>', text)

    # [text](url) is pulled out to a placeholder before the bare-URL
    # autolink pass runs, then stitched back in afterward: run in the
    # other order, autolink would also match the raw url sitting inside
    # the href="..." this substitution is about to produce, corrupting
    # the tag it just built rather than leaving it alone.
    links = []

    def _stash_link(m):
        links.append((m.group(1), m.group(2)))
        return f"\x00LINK{len(links) - 1}\x00"

    text = _BOARD_LINK_RE.sub(_stash_link, text)
    text = _BOARD_URL_RE.sub(
        lambda m: f'<a href="{m.group(1)}" rel="nofollow noopener noreferrer" target="_blank">{m.group(1)}</a>',
        text)
    for i, (link_text, url) in enumerate(links):
        text = text.replace(
            f"\x00LINK{i}\x00",
            f'<a href="{url}" rel="nofollow noopener noreferrer" target="_blank">{link_text}</a>')

    # Quote: a line starting with "> " (its escaped form, "&gt; ", since
    # escaping already ran) becomes its own <blockquote>, checked per
    # line after all inline formatting above so a quoted line can still
    # contain bold/code/a link.
    rendered_lines = []
    for line in text.split("\n"):
        m = _BOARD_QUOTE_LINE_RE.match(line)
        if m:
            rendered_lines.append(f"<blockquote>{m.group(1)}</blockquote>")
        else:
            rendered_lines.append(line)
    return Markup("<br>".join(rendered_lines))


def _parse_csv_outputs(outputs_raw):
    outputs, errors = [], []
    for i, line in enumerate(outputs_raw.strip().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(",")
        if len(parts) != 2:
            errors.append(f"Line {i}: expected 'address,amount'")
            continue
        addr, amt_str = parts[0].strip(), parts[1].strip()
        if addr.lower() == "burn":
            addr = crypto_mod.burn_address()
        if not crypto_mod.is_valid_address(addr):
            errors.append(f"Line {i}: invalid address")
            continue
        try:
            amt = int(amt_str)
        except ValueError:
            errors.append(f"Line {i}: invalid amount '{amt_str}'")
            continue
        if amt < 0:
            errors.append(f"Line {i}: amount must not be negative")
            continue
        if amt == 0:
            # A zero-amount output is never valid on the wire (tx_mod.validate
            # rejects it), so this isn't a real output. It's the untouched
            # half of a prefilled "address,0" line the sender left as-is.
            continue
        outputs.append({"to": addr, "amount": amt})
    return outputs, errors


_INSUFFICIENT_RE = re.compile(r"insufficient balance: have (\d+), need (\d+)")


def _reword_insufficient_balance(msg):
    """"insufficient balance: have 400000000, need 400001000" (ticks, the
    only unit consensus speaks) read back as "have 4 LAPSE 0 ticks, need
    4 LAPSE 1,000 ticks" so a person doesn't have to do the division
    themselves to see they're short."""
    m = _INSUFFICIENT_RE.search(msg)
    if not m:
        return msg
    have, need = (int(g) for g in m.groups())
    return (f"insufficient balance: have {fmt_balance(have)}, "
            f"need {fmt_balance(need)}")


def _base_dir():
    """Return the directory that contains templates_html/, working both from
    source (repo root) and inside a PyInstaller bundle (sys._MEIPASS)."""
    if getattr(sys, "frozen", False):
        return sys._MEIPASS
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")



def _tx_amount(t):
    """Total transfer amount for display."""
    return sum(o["amount"] for o in t.get("outputs", []))



def make_flask_app(import_name):
    """A Flask app set up the way every app here is: templates from the
    bundle, and the formatting helpers the templates call."""
    app = Flask(import_name,
                template_folder=os.path.join(_base_dir(), "templates_html"))
    app.jinja_env.globals.update(fmt_balance=fmt_balance, fmt_lapse=fmt_lapse,
                                 fmt_duration=fmt_duration,
                                 fmt_lapse_dp=fmt_lapse_dp,
                                 TICKS_PER_LAPSE=TICKS_PER_LAPSE,
                                 BURN_ADDRESS=crypto_mod.burn_address(),
                                 BOARD_POST_AMOUNT=tx_mod.BOARD_POST_AMOUNT,
                                 render_board_text=render_board_text,
                                 MAX_MEMO_BYTES=tx_mod.MAX_MEMO_BYTES)
    app.logger.setLevel(logging.WARNING)
    return app


def register_static_routes(app, pfx=""):
    """The favicon and the markdown toolbar script every page loads,
    served from the bundle rather than a CDN: a wallet's own UI shouldn't
    depend on a third-party host being reachable. See vendor/README.md."""

    @app.route("/favicon.svg", endpoint=pfx + "favicon")
    def favicon():
        # Served straight from the repo's actual lapsecoin.svg (rather than a
        # copy baked into the HTML) so the browser tab icon always matches
        # whatever the file on disk currently looks like.
        return send_file(os.path.join(_base_dir(), "lapsecoin.svg"),
                         mimetype="image/svg+xml", max_age=3600)

    @app.route("/vendor/markdown-toolbar-element.js", endpoint=pfx + "vendor_markdown_toolbar")
    def vendor_markdown_toolbar():
        return send_file(os.path.join(_base_dir(), "vendor", "markdown-toolbar-element.js"),
                         mimetype="application/javascript", max_age=86400)
