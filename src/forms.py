"""Making a form do its thing once.

A page that acts when its form is posted, and answers the POST with a page,
leaves the browser holding a POST. Reload it, or step Back and Forward, or
double click the button, and the browser sends it again. What it sent was a
payment, a post, an order. Two measures, which together settle it:

  1. Post/Redirect/Get. A POST is never answered with a page. It does what it
     does and answers 303 to the page it belongs on, so the page the browser
     is showing is a GET and reloading it only reads. What the POST has to say
     (the transaction was sent, the amount was wrong, here is the text you
     typed) rides along in a short-lived note held here, shown once.

  2. One-time tokens. Every form the server renders carries a token that
     works once. Whatever way a form gets sent twice, from a stale tab, the
     Back button, a double click, a replay, the second copy finds its token
     spent and nothing happens, and the page says so.

Light-safe: standard library and Flask only.
"""

import collections
import secrets
import threading
import time
from urllib.parse import urlencode

from flask import redirect

ALREADY_SENT = ("That form was already sent, or this app has restarted since the "
                "page was loaded. Nothing was sent again. Reload the page to start over.")


class OneTimeTokens:
    """Tokens good for one use each, kept in memory.

    They live a day: a page left open overnight still works, and one left
    for longer is told to reload. A restart forgets them all, which fails the
    safe way, as "already sent", never as a second send."""

    def __init__(self, ttl=24 * 3600, limit=4096):
        self._ttl, self._limit = ttl, limit
        self._lock = threading.Lock()
        # Oldest first. The lifetime is the same for every token, so the
        # oldest is always the first to expire.
        self._live = collections.OrderedDict()

    def issue(self):
        token = secrets.token_urlsafe(16)
        now = time.monotonic()
        with self._lock:
            while self._live and (next(iter(self._live.values())) <= now
                                  or len(self._live) >= self._limit):
                self._live.popitem(last=False)
            self._live[token] = now + self._ttl
        return token

    def consume(self, token):
        """True once for a token this server issued and has not seen used."""
        if not isinstance(token, str):
            return False
        with self._lock:
            expires = self._live.pop(token, None)
        return expires is not None and expires > time.monotonic()


class Notes:
    """What a POST wants the page it redirects to to show, held for a few
    minutes and handed over once. Keyed by an unguessable id in the URL, so
    the URL carries a reference and not the content, and a reload shows
    nothing stale."""

    def __init__(self, ttl=300, limit=256):
        self._ttl, self._limit = ttl, limit
        self._lock = threading.Lock()
        self._held = collections.OrderedDict()

    def keep(self, payload):
        note_id = secrets.token_hex(8)
        now = time.monotonic()
        with self._lock:
            while self._held and (next(iter(self._held.values()))[0] <= now
                                  or len(self._held) >= self._limit):
                self._held.popitem(last=False)
            self._held[note_id] = (now + self._ttl, payload)
        return note_id

    def take(self, note_id):
        """The payload for note_id, once, or {}."""
        if not isinstance(note_id, str):
            return {}
        with self._lock:
            held = self._held.pop(note_id, None)
        return held[1] if held and held[0] > time.monotonic() else {}


class Forms:
    def __init__(self):
        self.tokens = OneTimeTokens()
        self.notes = Notes()

    def done(self, path, payload=None, **query):
        """The answer to a POST: 303 to `path`, with `payload` (a dict) left
        for that page to show once."""
        if payload:
            query["note"] = self.notes.keep(payload)
        return redirect(path + ("?" + urlencode(query) if query else ""), code=303)


def forms_for(app):
    """The one Forms an app uses, made on first ask. Every route of an app
    that takes a form shares it, so a token issued by one page's GET is
    spent by that page's POST wherever the two are registered."""
    forms = app.extensions.get("forms")
    if forms is None:
        forms = app.extensions["forms"] = Forms()
    return forms
