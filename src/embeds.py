"""What a shared link looks like in Discord, Slack and the like.

Those apps read a page's Open Graph tags to draw the card under a pasted
link. Each page hands base.html one of the dicts built here, {title,
description}, made only from figures the page itself shows, so a friend
sees the block, transaction, balance or post that was shared and not a
generic site card. The icon and the card's colour are the same on every page
and live in base.html.

Nothing here reaches into a node: each builder takes plain values, so the
wording is testable without a chain. Light-safe: no node imports.
"""

import re
import time

from board_view import BOARD_MEMO_TAG, DELETE_TAG, VOTE_DOWN_TAG, VOTE_UP_TAG
from params import TICKS_PER_LAPSE
from ui_common import fmt_duration, fmt_lapse, fmt_lapse_dp

SITE = "LapseCoin"
TAGLINE = ("Peer-to-peer electronic cash, secured by a Verifiable Delay Function "
           "instead of proof-of-work mining.")
_TITLE_MAX = 80
_LINE_MAX = 120
_DESC_MAX = 260
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


def _clean(text, limit):
    """One line of plain text, cut at limit with an ellipsis. Board posts and
    nicknames come from anyone, so control characters and runs of space
    never reach a tag."""
    text = _CONTROL.sub(" ", str(text)).strip()
    text = re.sub(r" {2,}", " ", text)
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _embed(title, *lines):
    desc = "\n".join(_clean(line, _LINE_MAX) for line in lines if line)
    return {"title": _clean(title, _TITLE_MAX), "description": desc[:_DESC_MAX]}


def _count(n, one, many=None):
    return f"{n:,} {one if n == 1 else (many or one + 's')}"


def _lapse(ticks):
    """An amount a person can read: LAPSE to two places, or plain ticks when
    it is too small for that to say anything ("0.00 LAPSE" for 202 ticks)."""
    if 0 < ticks < TICKS_PER_LAPSE // 100:
        return f"{ticks:,} ticks"
    return fmt_lapse_dp(ticks, 2)


_HEADERS = re.compile(r"^(\[[a-z]:[^\]]*\])+")


def short_addr(addr):
    """The first three words of a twelve-word address, enough to tell
    addresses apart at a glance."""
    if not addr:
        return ""
    words = addr.split(".")
    return ".".join(words[:3]) + ("…" if len(words) > 3 else "")


def default():
    return _embed(SITE, TAGLINE)


def dashboard(info, supply_cap):
    pct = info["total_minted"] / supply_cap * 100 if supply_cap else 0
    return _embed(
        f"{SITE} at block {info['height']:,}",
        f"{fmt_lapse(info['circulating'])} circulating, {pct:.2f}% of the 21M cap minted",
        f"{_count(info['peer_count'], 'peer')} · {info['mempool_size']:,} pending · "
        f"{fmt_duration(info['network_age_seconds'])} since genesis")


def blocks(chain, now=None):
    """The three newest blocks, newest first."""
    now = time.time() if now is None else now
    lines = [f"#{b['height']:,} · {_count(len(b['transactions']), 'transaction')} · "
             f"{fmt_duration(now - b['timestamp'])} ago"
             for b in reversed(chain[-3:])]
    return _embed("Recent blocks", *lines)


def block(b, reward, time_stats):
    lines = [f"Built by {short_addr(b['builder'])}" if b.get("builder") else "Genesis block"]
    figures = [_count(len(b["transactions"]), "transaction")]
    if b.get("builder"):
        figures.insert(0, f"reward {fmt_lapse_dp(reward, 2)}")
    lines.append(" · ".join(figures))
    if time_stats:
        lines.append(f"Block time {time_stats['own']:.0f}s, median {time_stats['median']:.0f}s")
    lines.append(f"Hash {b['hash'][:16]}…")
    return _embed(f"Block {b['height']:,}", *lines)


def transaction(tx_hash, tx, location, gas_request=False):
    fee = f"fee {tx['fee']:,} ticks"
    memo = tx.get("memo", "")
    who = short_addr(tx["from"])
    # Board posts, votes and deletes are one-tick payments to the burn
    # address; what they say is the memo, not the amount.
    if memo.startswith(BOARD_MEMO_TAG):
        return _embed("Board post", f"{who}: {_HEADERS.sub('', memo[len(BOARD_MEMO_TAG):])}",
                      f"{location} · {fee}")
    if memo.startswith(VOTE_UP_TAG) or memo.startswith(VOTE_DOWN_TAG):
        direction = "up" if memo.startswith(VOTE_UP_TAG) else "down"
        return _embed(f"Board vote {direction}", f"{who} voted on post {memo.split(' ', 1)[1].strip()}",
                      f"{location} · {fee}")
    if memo.startswith(DELETE_TAG):
        return _embed("Board post deleted", f"{who} deleted post {memo[len(DELETE_TAG):].strip()}",
                      f"{location} · {fee}")
    outputs = tx.get("outputs", [])
    total = sum(o["amount"] for o in outputs)
    dest = short_addr(outputs[0]["to"]) if len(outputs) == 1 else _count(len(outputs), "recipient")
    return _embed("Fee request" if gas_request else f"Transaction {tx_hash[:12]}…",
                  f"{_lapse(total)} from {who} to {dest}", f"{location} · {fee}")


def address(addr, balance, tx_count):
    return _embed(short_addr(addr), _lapse(balance),
                  _count(tx_count, "transaction"))


def balance_lookup(holder_count):
    return _embed("Balance lookup",
                  "Look up any address or board nickname.",
                  f"{_count(holder_count, 'holder')} on chain" if holder_count else "")


def distribution(label, total):
    return _embed(f"Holders with {label}", _count(total, "address", "addresses"))


def mempool(rows):
    total = sum(sum(o["amount"] for o in r["tx"].get("outputs", [])) for r in rows)
    if not rows:
        return _embed("Mempool", "No pending transactions.")
    return _embed("Mempool", _count(len(rows), "pending transaction"),
                  f"{_lapse(total)} moving")


def network(peer_count):
    return _embed("Network", f"{_count(peer_count, 'peer')} connected",
                  "A live map of the nodes this node talks to.")


def odds(race, mining=True):
    if not race or race.get("odds_pct") is None:
        return _embed("Block race odds", "How likely a node is to win the next block.")
    field = (f"{_count(race['field_builders'], 'builder')} in the field, "
             f"median block {race['median']:.0f}s")
    if not mining:
        return _embed("Block race odds", "Not building right now: mining is off on this node.", field)
    return _embed("Block race odds",
                  f"This node's chance of the next block: {race['odds_pct']:.1f}%", field)


def benchmark(rows, counted, running_for):
    if not rows:
        return _embed("VDF benchmark", "How fast each declared machine computes VDFs.")
    top = rows[0]
    return _embed(
        "VDF benchmark",
        f"Fastest: {top['name']} at {top['ips']:,.0f} iterations per second",
        f"{_count(len(rows), 'machine')} ranked from {counted:,} blocks over {running_for}")


def whitepaper():
    return _embed(f"{SITE} whitepaper", TAGLINE)


def board(post_count, rows):
    """The count, and the newest top-level post as a teaser."""
    lines = [f"{_count(post_count, 'post')} on chain"]
    for row in rows:
        if not row.get("reply_ref"):
            who = row.get("nick") or short_addr(row["from"])
            lines.append(f"{who}: {row['text']}")
            break
    return _embed("Board", *lines)


def fees(network_count, cap_usd, lock_lapse):
    return _embed(
        "Fees: gas on other networks",
        f"A node sends you gas on {network_count} networks, up to about "
        f"${cap_usd:.0f} per request.",
        f"Deposit {lock_lapse} LAPSE")
