"""Board reading rules shared by the full node's web app and the light
client: parsing a post, threading, profiles, votes, and rendering context.
Light-safe: imports nothing that pulls in the VDF, the chain database, or
the swap code, and a test keeps it that way."""

import re

import tx as tx_mod


# Board tagging, burn amount and the fee-floor staircase are all consensus
# rules now (see tx.py: is_board_post, board_fee_floor and friends), not
# server-side UI defaults, so this module just aliases them.
BOARD_MEMO_TAG    = tx_mod.BOARD_MEMO_TAG


BOARD_POST_AMOUNT = tx_mod.BOARD_POST_AMOUNT


# Profile (icon + nickname) and reply-reference parsing/building now
# lives in tx.py, not here: the nickname a profile header carries is
# consensus-relevant (tx.validate()'s _check_nickname_available), so the
# code that decides what a post claims and the code that decides whether
# that claim is valid have to be the same one, or api.py's own idea of a
# memo's contents could quietly drift from what tx.py actually enforces.
parse_board_body  = tx_mod.parse_board_body


build_board_body  = tx_mod.build_board_body


REPLY_REF_LEN     = tx_mod.REPLY_REF_LEN


# Small built-in set so "icon" never means an uploaded image or a URL --
# both would cost far more bytes than this feature is worth and an <img>
# is exactly what render_board_text's whitelist refuses to ever emit.
# Index into this list is all a profile header carries; unknown/out of
# range indexes (an older client's palette was shorter, say) just fall
# back to the ghost placeholder rather than failing to render.
ICON_PALETTE = ["\U0001F47B", "\U0001F600", "\U0001F42C", "\U0001F984",
                "\U0001F41D", "\U0001F340", "\U0001F525", "\U0001F30A",
                "\U0001F31F", "\U0001F3AF", "\U0001F9E9", "\U0001F680",
                "\U0001F338", "\U0001F9CA", "\U0001F98A", "\U0001F989",
                "\U0001F42D", "\U0001F419", "\U0001F995", "\U0001F43C",
                "\U0001F98B", "\U0001F41B", "\U0001F340", "\U0001F32E"]


ICON_GHOST = "\U0001F47B"  # shown for an address with no profile post yet


def _icon_emoji(idx):
    """ICON_PALETTE[idx], or the ghost placeholder for an index outside
    it. tx.parse_board_body deliberately doesn't bounds-check icon (see
    its own docstring: that's a display concern, not a consensus one),
    so an older/newer client's differently sized palette, or simply
    nobody having set an icon at all, has to fall back safely here
    instead of indexing out of range.
    """
    return ICON_PALETTE[idx] if idx is not None and 0 <= idx < len(ICON_PALETTE) else ICON_GHOST


# Votes are ordinary transactions, not board posts: a different tag family
# entirely (tx.is_board_post only matches BOARD_MEMO_TAG), so voting never
# advances state.total_board_posts and never pays the board fee floor --
# just the same congestion-based fee as any other send, plus the same
# 1-tick burn a board post makes, on purpose (see conversation: kept equal
# so a vote is still a real, priced action, just never a rationed one).
VOTE_UP_TAG   = "[vote+] "


VOTE_DOWN_TAG = "[vote-] "


# Deleting a post is, like voting, an ordinary transaction under a tag of its
# own: not a board post, so it never advances the post count or pays the
# fee floor, only the same fee and 1-tick burn a vote does. The body is the
# 6-hex reference of the post to hide.
#
# Nothing here rewrites the chain, and nothing could: a deleted or edited
# post is still in its block for anyone who reads blocks. These are rules
# for readers, applied identically by every client that follows them, and
# the author check below is what keeps one person from deleting or editing
# another's words.
DELETE_TAG = "[del] "


# Editing a post is a board post whose text starts with an edit header, so
# it is a post in every sense that matters: it pays the board fee floor and
# burns the same tick, and it counts toward the floor's staircase. An old
# client that knows nothing of edits just shows it as a post.
#
#   [e:<6-hex ref>:<pos>:<ndel>]<inserted text>
#
# means: in the post with that reference, replace ndel characters at
# position pos with the inserted text. A splice, not a whole new text, so
# fixing a typo in a long post costs the header and a few characters, not
# the whole post again. Positions count characters, and each edit is
# applied to the post as the edits before it left it, in chain order.
_EDIT_RE = re.compile(r'^\[e:([0-9a-f]{6}):(\d{1,3}):(\d{1,3})\]')

# The most text a post may hold, edits included: the most a memo can carry
# less the tag every board post starts with. An original post cannot be
# longer than this, and repeated edits must not be a way past it.
MAX_POST_TEXT_BYTES = tx_mod.MAX_MEMO_BYTES - len(BOARD_MEMO_TAG)


def parse_board_edit(text):
    """(ref, pos, ndel, inserted) if text opens with an edit header, else
    None."""
    m = _EDIT_RE.match(text)
    if not m:
        return None
    return m.group(1), int(m.group(2)), int(m.group(3)), text[m.end():]


def build_board_edit(ref, pos, ndel, inserted):
    """Inverse of parse_board_edit: the text of an edit post."""
    return f"[e:{ref}:{pos}:{ndel}]{inserted}"


def make_splice(orig, new):
    """(pos, ndel, inserted) turning orig into new with a single splice:
    everything the two share at the front and at the back is left alone."""
    limit = min(len(orig), len(new))
    head = 0
    while head < limit and orig[head] == new[head]:
        head += 1
    tail = 0
    while tail < limit - head and orig[len(orig) - 1 - tail] == new[len(new) - 1 - tail]:
        tail += 1
    return head, len(orig) - head - tail, new[head:len(new) - tail]


def apply_splice(text, pos, ndel, inserted):
    """text with the splice applied, or None if it does not fit the text,
    leaves nothing, or would make the post longer than a post may be."""
    if pos > len(text) or pos + ndel > len(text):
        return None
    new = text[:pos] + inserted + text[pos + ndel:]
    if not new.strip() or len(new.encode("utf-8")) > MAX_POST_TEXT_BYTES:
        return None
    return new


def _nickname_owned_by(state, nick):
    """The address that owns nick (first board post to ever claim it,
    case-insensitively), or None if nobody has. Now a thin read of
    consensus state itself (tx.validate()'s _check_nickname_available
    enforces the exact same registry, see state.py), not a separate scan
    api.py used to run on its own. It is used to warn/refuse *before* a post
    pays for a nickname that would fail validation, not just to decide
    what to display after the fact.
    """
    return state.nicknames.get(nick.lower()) if nick else None


def _board_posts(chain):
    """Every board post on chain, tip first.

    A board post is an ordinary tx whose memo starts with BOARD_MEMO_TAG,
    so finding them means reading every transaction's memo, the same full
    scan address_lookup already does for a balance's history. Small
    enough a chain for that to be fine; if it stops being one, this is
    where to add an index.
    """
    rows = []
    for blk in reversed(chain):
        for t in reversed(blk.get("transactions", [])):
            memo = t.get("memo") or ""
            if memo.startswith(BOARD_MEMO_TAG):
                rows.append((blk["height"], blk.get("timestamp"),
                             tx_mod.tx_hash(t), t))
    return rows


def _board_profiles_and_votes(chain, nicknames, mempool=None):
    """One more full pass over the chain (see _board_posts' own docstring
    on why that's fine at this scale), building the two pieces of state a
    board post's header can affect but that no single post ever *is* by
    itself:

    profiles: addr -> {"icon", "nick"}, from the latest (tip-first, so
    first-seen-per-address) board post that address ever set a profile
    header on. Every rendered row looks its poster up here rather than
    trusting its own memo, so an old post always shows who its author
    currently is, not who they were when they wrote it, the same
    "avatar looked up live, not frozen at post time" behaviour any chat
    client gives you.

    nicknames: addr's claimed nick is only honored if it matches
    state.nicknames (passed in, not recomputed here), first-come-first-
    served, case-insensitive, enforced by tx.validate()'s own
    _check_nickname_available, so this is a read of the same consensus
    registry every node already maintains, not a separate, display-only
    notion of ownership that could disagree with it.

    votes: 6-hex tx-hash prefix -> {"up", "down"}, tallied from ordinary
    (non-board) VOTE_UP_TAG/VOTE_DOWN_TAG transactions anywhere on chain,
    plus (this is why mempool is a param) any of the same still sitting
    unconfirmed, counted first, exactly like a pending post already
    puts an unconfirmed post at the bottom of the feed instead of making
    the page look like the click did nothing until a block lands. One
    vote per (address, target) survives, not one per transaction: a vote
    isn't a repeatable action that piles up, it's a single choice that
    can change your mind, exactly like Remark42's own model (a vote there
    is one stored value per user per comment, overwritten by a later
    click, never summed). Walking mempool-then-tip-first and keeping only
    the first vote seen per (address, target) pair gets that same
    "latest replaces, doesn't add" semantics for free, the same trick
    profiles above already uses for "latest icon/nickname wins".

    pending_vote_refs: the set of target refs with an unconfirmed vote
    counted above, so a row can show its score as still-settling instead
    of implying a mined, final number.
    """
    profiles = {}
    votes = {}
    voted = set()  # (address, target ref) already counted, most recent/pending first
    pending_vote_refs = set()

    def _tally_vote(t):
        """Counts t if it's a vote, returning the ref it targeted (so the
        caller can mark that ref pending) or None if it wasn't a vote at
        all."""
        memo = t.get("memo") or ""
        if not (memo.startswith(VOTE_UP_TAG) or memo.startswith(VOTE_DOWN_TAG)):
            return None
        up = memo.startswith(VOTE_UP_TAG)
        tag = VOTE_UP_TAG if up else VOTE_DOWN_TAG
        ref = memo[len(tag):len(tag) + REPLY_REF_LEN]
        voter = t.get("from")
        key = (voter, ref)
        if key in voted:
            return ref
        voted.add(key)
        tally = votes.setdefault(ref, {"up": 0, "down": 0, "by": {}})
        tally["up" if up else "down"] += 1
        tally["by"][voter] = "up" if up else "down"
        return ref

    if mempool is not None:
        for t in mempool.all_txs():
            ref = _tally_vote(t)
            if ref is not None:
                pending_vote_refs.add(ref)

    for blk in reversed(chain):
        for t in reversed(blk.get("transactions", [])):
            memo = t.get("memo") or ""
            if memo.startswith(BOARD_MEMO_TAG):
                addr = t.get("from")
                if addr in profiles:
                    continue
                icon, nick, _, _ = parse_board_body(memo[len(BOARD_MEMO_TAG):])
                if nick and nicknames.get(nick.lower()) != addr:
                    nick = None  # claimed by a different address; see state.nicknames
                if icon is not None:
                    profiles[addr] = {"icon": icon, "nick": nick}
            else:
                _tally_vote(t)
    return profiles, votes, pending_vote_refs




def _enrich_board_row(row, profiles, votes, hash6_index, own_addr=None, pending_vote_refs=frozenset()):
    """Attach everything board.html actually renders for one row: the
    poster's current icon/nickname (looked up live, see
    _board_profiles_and_votes), this post's own vote tally, whether that
    tally still has an unconfirmed vote in it (so the count can read as
    still-settling instead of implying a final, mined number, the same
    "pending..." honesty a freshly posted message already gets), the
    viewer's own prior vote if any (so the matching button can show the
    same already-voted, disabled state Remark42's own CommentVotes does),
    and a reply preview if it has one, so the template only ever reads
    plain fields off row, never re-parses a memo itself.

    The row already carries its resolved text (edits applied, deleted
    posts emptied, see resolve_board), so nothing here reads a memo.
    """
    prof = profiles.get(row["from"])
    row["icon"] = _icon_emoji(prof["icon"]) if prof else ICON_GHOST
    row["nick"] = prof.get("nick") if prof else None
    row["ref6"] = row["hash"][:REPLY_REF_LEN]
    tally = votes.get(row["ref6"], {"up": 0, "down": 0, "by": {}})
    row["up"], row["down"] = tally["up"], tally["down"]
    row["my_vote"] = tally.get("by", {}).get(own_addr)
    row["vote_pending"] = row["ref6"] in pending_vote_refs
    row["reply_from"] = row["reply_snippet"] = None
    if row["reply_ref"]:
        target = hash6_index.get(row["reply_ref"])
        if target is not None:
            row["reply_from"] = target["from"]
            row["reply_snippet"] = target["text"][:60]
            row["reply_deleted"] = target.get("deleted", False)
    return row


BOARD_MAX_DEPTH = 4


BOARD_THREADS_PER_CHUNK = 10


def _flatten_threads(entries, score_of):
    """All threads in display order as [(entry, depth)], plus the index in
    that list where each thread starts. Does not mutate entries."""
    by_ref = {e["ref6"]: e for e in entries}
    seq = {id(e): i for i, e in enumerate(entries)}
    children, roots = {}, []
    for e in entries:
        parent = by_ref.get(e["reply_ref"]) if e["reply_ref"] else None
        if parent is None or parent is e:
            roots.append(e)
        else:
            children.setdefault(parent["ref6"], []).append(e)
    roots.reverse()                       # newest thread first
    flat, starts, seen = [], [], set()

    def walk(e, depth):
        if e["ref6"] in seen:
            return
        seen.add(e["ref6"])
        flat.append((e, min(depth, BOARD_MAX_DEPTH)))
        kids = children.get(e["ref6"], [])
        kids.sort(key=lambda c: (-score_of(c), -seq[id(c)]))
        for c in kids:
            walk(c, depth + 1)

    for r in roots:
        starts.append(len(flat))
        walk(r, 0)
    return flat, starts


# Memo tags a board's state depends on: posts themselves, and the votes that
# score them. Anything else in the mempool cannot change what the board shows.
BOARD_TAGS = (BOARD_MEMO_TAG, VOTE_UP_TAG, VOTE_DOWN_TAG, DELETE_TAG)


def _board_events(chain, mempool):
    """Every board post and every delete, oldest first: confirmed ones in
    chain order, then the ones still in the mempool (ordered by sender and
    nonce, the only order a sender's own pending transactions have)."""
    events = []
    for blk in chain:
        for t in blk.get("transactions", []):
            memo = t.get("memo") or ""
            if memo.startswith((BOARD_MEMO_TAG, DELETE_TAG)):
                events.append({"height": blk["height"], "ts": blk.get("timestamp"),
                               "hash": tx_mod.tx_hash(t), "tx": t, "pending": False})
    pending = [t for t in mempool.all_txs()
               if (t.get("memo") or "").startswith((BOARD_MEMO_TAG, DELETE_TAG))]
    pending.sort(key=lambda t: (t.get("from") or "", t.get("nonce") or 0))
    for t in pending:
        events.append({"height": None, "ts": None, "hash": tx_mod.tx_hash(t),
                       "tx": t, "pending": True})
    return events


def resolve_board(events):
    """The posts a reader sees, from the events behind them: edits applied,
    deleted posts emptied, and the edits and deletes themselves gone from
    the feed, having done their work. Returns posts oldest first.

    An edit or a delete acts only on a post by its own sender, and only on
    one that came before it; the post it means is the sender's most recent
    one with that reference. An edit that cannot be applied (no such post,
    not the sender's, a splice that does not fit, a result too long or
    empty) is not hidden: it is shown as the ordinary post it also is. An
    edit of a post already deleted is dropped, there being nothing to show
    it on.
    """
    posts = []
    by_author_ref = {}

    def latest(author, ref):
        found = by_author_ref.get((author, ref))
        return found[-1] if found else None

    for ev in events:
        t = ev["tx"]
        memo = t.get("memo") or ""
        author = t.get("from")
        if memo.startswith(DELETE_TAG):
            target = latest(author, memo[len(DELETE_TAG):][:REPLY_REF_LEN])
            if target is not None:
                target["deleted"] = True
                target["text"] = ""
            continue
        _icon, _nick, reply_ref, text = parse_board_body(memo[len(BOARD_MEMO_TAG):])
        edit = parse_board_edit(text)
        if edit is not None:
            ref, pos, ndel, inserted = edit
            target = latest(author, ref)
            if target is not None:
                if target["deleted"]:
                    continue
                new = apply_splice(target["text"], pos, ndel, inserted)
                if new is not None:
                    target["text"] = new
                    target["edited"] = True
                    continue
        entry = {"height": ev["height"], "ts": ev["ts"], "hash": ev["hash"],
                 "pending": ev["pending"], "from": author, "text": text,
                 "reply_ref": reply_ref, "ref6": ev["hash"][:REPLY_REF_LEN],
                 "edited": False, "deleted": False}
        posts.append(entry)
        by_author_ref.setdefault((author, entry["ref6"]), []).append(entry)
    return posts


def build_board_snapshot(chain, nicknames, mempool):
    """The whole board, resolved: everything board_page_data slices from.

    Returns (flat, starts, profiles, votes, pending_vote_refs, hash6_index,
    post_count). Reads the whole chain once; callers cache it per chain
    and mempool state, see LocalReader."""
    events = _board_events(chain, mempool)
    entries = resolve_board(events)
    profiles, votes, pending_vote_refs = _board_profiles_and_votes(
        chain, nicknames, mempool)
    hash6_index = {e["ref6"]: e for e in entries}
    post_count = sum(1 for ev in events if not ev["pending"]
                     and (ev["tx"].get("memo") or "").startswith(BOARD_MEMO_TAG))

    def score_of(row):
        tally = votes.get(row["ref6"])
        return (tally["up"] - tally["down"]) if tally else 0

    flat, starts = _flatten_threads(entries, score_of)
    return (flat, starts, profiles, votes, pending_vote_refs, hash6_index,
            post_count)


def board_page_data(snapshot, chunks):
    """The first `chunks` chunks of the board, as plain JSON-able data and
    nothing beyond what those rows need: the posters' profiles, the vote
    tallies of the rows shown, and the posts those rows reply to. A row is
    its resolved text and who wrote it, never the transaction behind it:
    that carries a public key and a signature, over two kilobytes of hex
    that nothing on the page shows.

    This is the one shape the board is exchanged in. A full node builds it
    from its own snapshot, and serves it as /api/board/page, and a light
    client reads that same dict from a remote node, so both feed the same
    rendering code."""
    flat, starts, profiles, votes, pending_vote_refs, hash6_index, post_count = snapshot
    limit = max(chunks, 1) * BOARD_THREADS_PER_CHUNK
    cut = starts[limit] if limit < len(starts) else len(flat)
    rows, refs, posters, targets = [], set(), set(), {}
    for entry, depth in flat[:cut]:
        rows.append({"height": entry["height"], "ts": entry["ts"],
                     "hash": entry["hash"], "pending": entry["pending"],
                     "depth": depth, "from": entry["from"], "text": entry["text"],
                     "reply_ref": entry["reply_ref"], "edited": entry["edited"],
                     "deleted": entry["deleted"]})
        refs.add(entry["ref6"])
        posters.add(entry["from"])
        if entry["reply_ref"]:
            target = hash6_index.get(entry["reply_ref"])
            if target is not None:
                targets[entry["reply_ref"]] = {"from": target["from"],
                                               "text": target["text"][:60],
                                               "deleted": target["deleted"]}
    return {"rows": rows,
            "profiles": {a: profiles[a] for a in posters if a in profiles},
            "votes": {r: votes[r] for r in refs if r in votes},
            "pending_vote_refs": sorted(refs & set(pending_vote_refs)),
            "targets": targets,
            "post_count": post_count,
            "has_more": limit < len(starts)}


def board_ctx(reader, page_arg, own_addr, extra=None):
    """Board page context. The board is one continuous feed (no page
    cuts): newest thread first, each thread kept whole with replies nested
    under their parent. `page_arg` is how many chunks of
    BOARD_THREADS_PER_CHUNK threads to render, so the page can keep
    appending as the reader scrolls. Shared between the GET route
    (read-only, both apps), the private app's POST handlers, and the light
    client, so none of them can drift apart.

    own_addr is the caller's call, not this function's: the wallet's own
    address on the private app and the light client, and on the public app
    whatever the Dandelion privacy setting (settings.HIDE_ADDRESS_PUBLICLY)
    says, possibly None, which never matches a real row.tx.from and so
    quietly drops the "mine" styling in board.html rather than needing
    its own branch here.

    The viewer's own icon and nickname come from reader.profile(), which a
    light client answers from the board it already has, so showing them
    never tells a node whose address this is.
    """
    chunks = max(page_arg, 1)
    data = reader.board_page(chunks)
    pending_vote_refs = frozenset(data["pending_vote_refs"])
    # Copies: enrichment (own_addr-dependent "mine", etc.) is per request.
    page_rows = [dict(r) for r in data["rows"]]
    for row in page_rows:
        _enrich_board_row(row, data["profiles"], data["votes"], data["targets"],
                          own_addr, pending_vote_refs)
    own_profile = reader.profile(own_addr) if own_addr else None
    own_icon_idx = own_profile["icon"] if own_profile else None
    ctx = dict(title="Board", rows=page_rows,
               post_count=data["post_count"], own_addr=own_addr,
               own_icon=_icon_emoji(own_icon_idx),
               own_icon_idx=own_icon_idx or 0,
               own_nick=own_profile.get("nick") if own_profile else None,
               icon_palette=ICON_PALETTE,
               tag_len=len(BOARD_MEMO_TAG),
               page=chunks, has_more=data["has_more"])
    if extra:
        ctx.update(extra)
    return ctx
