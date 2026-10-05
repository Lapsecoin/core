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

from remote_reader import DEFAULT_SEEDS, RemoteReader, default_cache_file
from version import LOCAL_VERSION
from wallet import Wallet, export_key, key_path, load_or_create_key


def _serve(app, host, port):
    # waitress, not Flask's development server, for the same reason the
    # full node uses it: pure Python, bundles under PyInstaller.
    from waitress import serve
    serve(app, host=host, port=port, threads=4, ident=None, channel_timeout=60)


def build_parser():
    p = argparse.ArgumentParser(
        prog="lapsecoin-dumb",
        description="LapseCoin light client: wallet and board through another node.")
    p.add_argument("--key", "--keyfile", dest="keyfile", default=None,
                   help="The wallet key: a path to the key file, or the text --export "
                        "prints. A key from the full node works as is. Default: "
                        "$LAPSECOIN_KEY, else lapsecoin_key.json.")
    p.add_argument("--export", action="store_true",
                   help="Print the key as one line of text (still encrypted) and exit.")
    p.add_argument("--node", action="append", default=[], metavar="URL",
                   help="A node to ask (repeatable). Default: " + ", ".join(DEFAULT_SEEDS))
    p.add_argument("--port", type=int, default=8335, help="Local port for the wallet.")
    p.add_argument("--proxy", default="auto", metavar="auto|none|URL",
                   help="auto (the default) uses Tor whenever it is running, and stops when it "
                        "is not; none never uses a proxy; a URL such as socks5h://127.0.0.1:9050 "
                        "always uses that one.")
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
    try:
        args.keyfile = key_path(args.keyfile)
    except ValueError as e:
        sys.exit(f"--key: {e}")
    if args.export:
        print(export_key(args.keyfile))
        return
    pk, _kek, _passphrase = load_or_create_key(args.keyfile)
    wallet = Wallet(args.keyfile, pk)
    if args.proxy not in ("auto", "none") and "://" not in args.proxy:
        sys.exit("--proxy: use auto, none, or a proxy URL such as socks5h://127.0.0.1:9050")
    reader = RemoteReader(args.node or None,
                          proxy=None if args.proxy == "none" else args.proxy,
                          refresh=args.refresh, cache_file=default_cache_file())
    from light_app import create_light_app
    app = create_light_app(reader, wallet, port=args.port)
    url = f"http://127.0.0.1:{args.port}/"
    print("Looking at the board names nobody. Your wallet address is sent only to look up "
          "a balance or to send, by the most private route there is: Tor if it is running, "
          "else through a relay when two nodes support it, else straight to a node. The page "
          "header says which.\n")
    for n in args.node:
        if n.startswith("http://") and urlsplit(n).hostname not in ("127.0.0.1", "localhost", "::1"):
            print(f"Warning: --node {n} is plain http, so your wallet address crosses "
                  "the network unencrypted if it is sent there directly.\n")
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
