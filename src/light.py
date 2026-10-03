"""LapseCoin light client: a wallet and the board, with no chain of its own.

It holds your key and signs here. Everything it needs to know it asks of
another node's public API, and it keeps what it asks to a minimum, so it
runs where a full node cannot: a small data allowance, a small disk, no
open ports, no mining. The cost of that is trust in the node it asks for
what the chain says (it can lie about a balance, never spend it), and that
the node sees your address when you look at it. Use --proxy to route
through Tor or another proxy if that matters.
"""

import argparse
import logging
import sys
import threading
import webbrowser
from urllib.parse import urlsplit

from remote_reader import DEFAULT_SEEDS, RemoteError, RemoteReader, default_cache_file
from version import LOCAL_VERSION
from wallet import Wallet, load_or_create_key


def _serve(app, host, port):
    # waitress, not Flask's development server, for the same reason the
    # full node uses it: pure Python, bundles under PyInstaller.
    from waitress import serve
    serve(app, host=host, port=port, threads=4, ident=None, channel_timeout=60)


def build_parser():
    p = argparse.ArgumentParser(
        prog="lapsecoin-dumb",
        description="LapseCoin light client: wallet and board through another node.")
    p.add_argument("--keyfile", default="lapsecoin_key.json",
                   help="The wallet key. A key from the full node works as is.")
    p.add_argument("--node", action="append", default=[], metavar="URL",
                   help="A node to ask (repeatable). Default: " + ", ".join(DEFAULT_SEEDS))
    p.add_argument("--port", type=int, default=8335, help="Local port for the wallet.")
    p.add_argument("--proxy", default=None, metavar="URL",
                   help="Send every request through a proxy so the node does not see your IP. "
                        "'tor' uses Tor's local port (the daemon or Tor Browser); or give a URL, "
                        "e.g. socks5h://127.0.0.1:9050.")
    p.add_argument("--allow-plain-http", action="store_true",
                   help="Send your wallet address to nodes over plain http too. By default "
                        "it goes only to nodes reached over https, and to ones you name with "
                        "--node. Plain http lets anyone on the network path read it.")
    p.add_argument("--refresh", type=int, default=15, metavar="SECONDS",
                   help="How long an answer is reused before the node is asked "
                        "again. Raise it to use less data.")
    p.add_argument("--no-browser", action="store_true",
                   help="Do not open the wallet in a browser.")
    p.add_argument("--version", action="version", version=f"lapsecoin-dumb {LOCAL_VERSION}")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    pk, _kek, _passphrase = load_or_create_key(args.keyfile)
    wallet = Wallet(args.keyfile, pk)
    try:
        reader = RemoteReader(args.node or None, proxy=args.proxy, refresh=args.refresh,
                              cache_file=default_cache_file(),
                              allow_plain_http=args.allow_plain_http)
    except RemoteError as e:
        sys.exit(str(e))
    from light_app import create_light_app
    app = create_light_app(reader, wallet, port=args.port)
    url = f"http://127.0.0.1:{args.port}/"
    if not args.proxy:
        print("Note: the node you ask sees your IP address together with your wallet "
              "address, whenever you open Balance or Send or post. Reading the board "
              "does not name you.\nUse --proxy tor to hide your IP, or --node with a "
              "node you run yourself.\n")
    if not args.proxy:
        if args.allow_plain_http:
            print("Warning: --allow-plain-http. Your wallet address may be sent to nodes "
                  "unencrypted, where anyone on the network path can read it.\n")
        else:
            print("Your wallet address goes only to nodes reached over https, or ones you "
                  "name with --node.\n")
        loose = [n for n in args.node if n.startswith("http://")
                 and not urlsplit(n).hostname in ("127.0.0.1", "localhost", "::1")]
        for n in loose:
            print(f"Warning: --node {n} is plain http, so your wallet address crosses "
                  "the network unencrypted to reach it.\n")
    print(f"Your address: {wallet.addr}")
    print(f"Wallet: {url}   (Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        _serve(app, "127.0.0.1", args.port)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
