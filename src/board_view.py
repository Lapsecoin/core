"""Board reading rules shared by the full node's web app and the light
client: parsing a post, threading, profiles, votes, and rendering context.
Light-safe: imports nothing that pulls in the VDF, the chain database, or
the swap code, and a test keeps it that way."""

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


def _nickname_owned_by(state, nick):
    """The address that owns nick (first board post to ever claim it,
    case-insensitively), or None if nobody has. Now a thin read of
    consensus state itself (tx.validate()'s _check_nickname_available
    enforces the exact same registry, see state.py), not a separate scan
    api.py used to run on its own -- used to warn/refuse *before* a post
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
    currently is, not who they were when they wrote it -- the same
    "avatar looked up live, not frozen at post time" behaviour any chat
    client gives you.

    nicknames: addr's claimed nick is only honored if it matches
    state.nicknames (passed in, not recomputed here) -- first-come-first-
    served, case-insensitive, enforced by tx.validate()'s own
    _check_nickname_available, so this is a read of the same consensus
    registry every node already maintains, not a separate, display-only
    notion of ownership that could disagree with it.

    votes: 6-hex tx-hash prefix -> {"up", "down"}, tallied from ordinary
    (non-board) VOTE_UP_TAG/VOTE_DOWN_TAG transactions anywhere on chain,
    plus (this is why mempool is a param) any of the same still sitting
    unconfirmed -- counted first, exactly like _board_pending already
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


def _board_pending(mempool):
    """Board-tagged txs sitting in the mempool, not yet mined: the same
    "waiting to be mined" fact a compose-time alert used to state in
    words, shown instead as a row in the feed itself, since the mempool
    already knows this and a separate banner was just repeating it less
    usefully. No ordering guarantee among these (nothing pre-confirmation
    has one), which is fine, they're always the newest thing on the page
    regardless of the order a few of them happen to render in.
    """
    rows = []
    for t in mempool.all_txs():
        memo = t.get("memo") or ""
        if memo.startswith(BOARD_MEMO_TAG):
            rows.append({"height": None, "ts": None,
                         "hash": tx_mod.tx_hash(t), "tx": t, "pending": True})
    return rows


def _enrich_board_row(row, profiles, votes, hash6_index, own_addr=None, pending_vote_refs=frozenset()):
    """Attach everything board.html actually renders for one row -- the
    poster's current icon/nickname (looked up live, see
    _board_profiles_and_votes), this post's own vote tally, whether that
    tally still has an unconfirmed vote in it (so the count can read as
    still-settling instead of implying a final, mined number -- the same
    "pending..." honesty a freshly posted message already gets), the
    viewer's own prior vote if any (so the matching button can show the
    same already-voted, disabled state Remark42's own CommentVotes does),
    and a reply preview if it has one -- so the template only ever reads
    plain fields off row, never re-parses a memo itself.
    """
    memo = row["tx"].get("memo") or ""
    icon, _nick, reply_ref, text = parse_board_body(memo[len(BOARD_MEMO_TAG):])
    prof = profiles.get(row["tx"].get("from"))
    row["icon"] = _icon_emoji(prof["icon"]) if prof else ICON_GHOST
    row["nick"] = prof.get("nick") if prof else None
    row["text"] = text
    row["ref6"] = row["hash"][:REPLY_REF_LEN]
    tally = votes.get(row["ref6"], {"up": 0, "down": 0, "by": {}})
    row["up"], row["down"] = tally["up"], tally["down"]
    row["my_vote"] = tally.get("by", {}).get(own_addr)
    row["vote_pending"] = row["ref6"] in pending_vote_refs
    row["reply_ref"] = reply_ref
    row["reply_from"] = row["reply_snippet"] = None
    if reply_ref:
        target = hash6_index.get(reply_ref)
        if target is not None:
            row["reply_from"] = target["tx"].get("from")
            _, _, _, target_text = parse_board_body(
                (target["tx"].get("memo") or "")[len(BOARD_MEMO_TAG):])
            row["reply_snippet"] = target_text[:60]
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
BOARD_TAGS = (BOARD_MEMO_TAG, VOTE_UP_TAG, VOTE_DOWN_TAG)


def build_board_snapshot(chain, nicknames, mempool):
    """The whole board, resolved: everything board_page_data slices from.

    Returns (flat, starts, profiles, votes, pending_vote_refs, hash6_index,
    post_count). Reads the whole chain once; callers cache it per chain
    and mempool state, see LocalReader."""
    all_rows = _board_posts(chain)                    # tip-first
    entries = [{"height": h, "ts": ts, "hash": hsh, "tx": t, "pending": False}
               for h, ts, hsh, t in reversed(all_rows)]
    pending_rows = _board_pending(mempool)
    entries += pending_rows                            # always newest
    profiles, votes, pending_vote_refs = _board_profiles_and_votes(
        chain, nicknames, mempool)
    hash6_index = {h[:REPLY_REF_LEN]: {"tx": t} for _, _, h, t in all_rows}
    for row in pending_rows:
        hash6_index.setdefault(row["hash"][:REPLY_REF_LEN], {"tx": row["tx"]})
    for row in entries:
        row["ref6"] = row["hash"][:REPLY_REF_LEN]
        row["reply_ref"] = parse_board_body(
            (row["tx"].get("memo") or "")[len(BOARD_MEMO_TAG):])[2]

    def score_of(row):
        tally = votes.get(row["ref6"])
        return (tally["up"] - tally["down"]) if tally else 0

    flat, starts = _flatten_threads(entries, score_of)
    return (flat, starts, profiles, votes, pending_vote_refs, hash6_index,
            len(all_rows))


def _lean_tx(t):
    """The two fields a board row is rendered from. A full transaction
    carries a public key and a signature, over two kilobytes of hex that
    nothing on the page shows."""
    return {"from": t.get("from"), "memo": t.get("memo") or ""}


def board_page_data(snapshot, chunks):
    """The first `chunks` chunks of the board, as plain JSON-able data and
    nothing beyond what those rows need: the posters' profiles, the vote
    tallies of the rows shown, and the posts those rows reply to.

    This is the one shape the board is exchanged in. A full node builds it
    from its own snapshot, and serves it as /api/board/page, and a light
    client reads that same dict from a remote node, so both feed the same
    rendering code."""
    flat, starts, profiles, votes, pending_vote_refs, hash6_index, post_count = snapshot
    limit = max(chunks, 1) * BOARD_THREADS_PER_CHUNK
    cut = starts[limit] if limit < len(starts) else len(flat)
    rows, refs, posters, targets = [], set(), set(), {}
    for entry, depth in flat[:cut]:
        t = entry["tx"]
        rows.append({"height": entry["height"], "ts": entry["ts"],
                     "hash": entry["hash"], "pending": entry["pending"],
                     "depth": depth, "tx": _lean_tx(t)})
        refs.add(entry["ref6"])
        posters.add(t.get("from"))
        if entry["reply_ref"]:
            target = hash6_index.get(entry["reply_ref"])
            if target is not None:
                targets[entry["reply_ref"]] = {"tx": _lean_tx(target["tx"])}
    return {"rows": rows,
            "profiles": {a: profiles[a] for a in posters if a in profiles},
            "votes": {r: votes[r] for r in refs if r in votes},
            "pending_vote_refs": sorted(refs & set(pending_vote_refs)),
            "targets": targets,
            "post_count": post_count,
            "has_more": limit < len(starts)}


def board_ctx(reader, page_arg, own_addr, extra=None, own_profile=None):
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

    own_profile is the viewer's current {"icon", "nick"}, if any: it is
    about the viewer, not the page, so it is not part of the page data.
    """
    chunks = max(page_arg, 1)
    data = reader.board_page(chunks)
    pending_vote_refs = frozenset(data["pending_vote_refs"])
    # Copies: enrichment (own_addr-dependent "mine", etc.) is per request.
    page_rows = [dict(r) for r in data["rows"]]
    for row in page_rows:
        _enrich_board_row(row, data["profiles"], data["votes"], data["targets"],
                          own_addr, pending_vote_refs)
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
