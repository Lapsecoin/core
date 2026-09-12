<div align="center">
  <img src="lapsecoin.svg" width="120" alt="LapseCoin logo" />

  # LapseCoin

  Peer-to-peer electronic cash, secured by a Verifiable Delay Function instead of proof-of-work mining, with quantum-resistant signatures.

  [![Release](https://img.shields.io/github/v/release/Vic-Nas/lapsecoin)](https://github.com/Vic-Nas/lapsecoin/releases)
  [![Live node](https://img.shields.io/badge/node-lapsenode.vicnas.me-2ea44f)](https://lapsenode.vicnas.me/)
  [![Whitepaper](https://img.shields.io/badge/docs-whitepaper-blue)](docs/whitepaper.md)
  [![Donate BTC](https://img.shields.io/badge/donate-BTC-f7931a)](#support)
  [![Discord](https://img.shields.io/badge/discord-join-5865F2?logo=discord&logoColor=white)](https://discord.gg/FP2d8JmK6r)
</div>

**Recommended: use a pre-built release.** Building from source requires native libraries (liboqs, chiavdf) that involve complex C/C++ compilation and can produce DLL or shared library errors depending on your platform. The release binaries on the [releases page](https://github.com/Vic-Nas/lapsecoin/releases) are self-contained and require no dependencies.

Join the [Discord](https://discord.gg/FP2d8JmK6r) for discussions, news, and trades with other coins.

## Quick start

Grab a binary from the [releases page](https://github.com/Vic-Nas/lapsecoin/releases), self-contained with no dependencies.

```
# Linux                    # Windows
chmod +x lapsecoin         lapsecoin.exe
./lapsecoin
```

You'll be prompted for a signing passphrase, then the wallet is at `http://localhost:8335` and the block explorer at `http://localhost:8333`.

<details>
<summary>How consensus works</summary>

Most cumulative proven work wins, Bitcoin-style. Two blocks at the same height carry equal work, so that tie goes to the lower VDF output, not to whichever arrived first. A height stays open to a better sibling for a few seconds (`LAPSECOIN_DRAW_WINDOW_SECONDS`) so the comparison can happen; work on the next height never stops meanwhile.

Block timing is enforced by a VDF anchored to real elapsed time, believed to have a much smaller hardware-advantage gap than proof-of-work. Transactions are ordinary and plaintext, with sender-bid fees, much like Bitcoin's own. Signatures are FALCON-512 (quantum-resistant). Full spec in [docs/whitepaper.md](docs/whitepaper.md).
</details>

<details>
<summary>Running from source</summary>

Requires Python 3.11+ and native build dependencies for your platform (liboqs, chiavdf).

```
pip install -r requirements.txt
python main.py
```

If that fails on `libtorrent` specifically: it's a C extension with real wheel gaps on some platforms (Windows in particular, where a new Python release regularly goes months without a published one), so it can be the one line standing between you and a working install. Delete it from `requirements.txt` and install everything else, then get a `lapsecoin_peers.json` from someone else's running node (their peers page has a "Download JSON" button, `/api/peers/download`) and drop it in your own node's working directory before starting. This node reads that file to seed known-good addresses on startup regardless of `libtorrent`, so you still connect without it, just via a peer someone handed you instead of the DHT finding one on its own.
</details>

<details>
<summary>Building the binary yourself</summary>

```
pip install pyinstaller cairosvg Pillow miniupnpc
make linux    # on Linux
make windows  # on Windows
```

Produces a self-contained binary in `dist/`. Requires cmake, ninja, and a C compiler (on Windows, also liboqs and MSVC redistributables).
</details>

<details>
<summary>Updating, and when it isn't optional</summary>

A node too old to speak the current wire format is refused at the handshake, so it sits alone mining a chain nobody sees. Old formats aren't carried forever, so some updates are mandatory. The version number says which:

| Change | Example | What it means |
|---|---|---|
| Third number | `0.6.0` to `0.6.1` | Fixes. Update when convenient |
| Second number | `0.5.1` to `0.6.0` | **Required.** Wire format changed, older nodes are dropped |
| First number | `0.x` to `1.x` | Consensus break. Required, expect a resync |

Alone with no peers after everyone else updated? Check your version first.
</details>

<details>
<summary>Ports and passphrase</summary>

| | Port | Interface | Purpose |
|---|---|---|---|
| Public | `8333` (`--port`) | `0.0.0.0` | Peer traffic (UDP) and the read-only node UI (TCP). Safe to expose. Send disabled. |
| Private | `port+2` (`--private-port`) | `127.0.0.1` | Wallet UI. **Never expose.** Full access, including Send. |

`port+3` is reserved for the DHT subsystem (libtorrent). Port `18334` is fixed and reserved across every node for same-network peer discovery (broadcast-based, finds other LapseCoin nodes on your LAN automatically regardless of their own port). Don't bind other services to either.

The passphrase is required to start the node. By default you're prompted via `getpass` (nothing touches shell history or `ps`). For Docker/systemd/CI, set it non-interactively instead:

```bash
export LAPSECOIN_PASSPHRASE="your passphrase"
python main.py
```

There is no `--passphrase` flag, since it was removed because it leaked into `ps aux` and shell history.

The peer port can be set the same way, which is often easier than a flag in a container or unit file:

```bash
export LAPSECOIN_PORT=8444
python main.py
```

`--port` still wins if you pass it, so the variable sets the default rather than overriding what you typed. The private port follows from it as usual unless you set `--private-port`.
</details>

<details>
<summary>Settings and environment variables</summary>

Node-local settings live on the private wallet UI under **Settings**, and each can also be set by environment variable. The environment wins, so a container or unit file can force a value for one launch without overwriting what's saved; a value set that way shows on the page as forced rather than editable.

| Variable | Default | Meaning |
|---|---|---|
| `LAPSECOIN_PRIVATE_ADDRESS` | `false` | Advertise a separate address to peers instead of the one this node builds blocks with |
| `LAPSECOIN_ADVERTISED_ADDRESS` | *(generated)* | Advertise this specific address instead of the generated one |
| `LAPSECOIN_DRAW_WINDOW_SECONDS` | `10` | How long a height keeps accepting a better same-height block. Anything finishing inside it is treated as a tie and decided on proof rather than speed, so this is also how much of a speed advantage it takes to win a height outright. Work on the next height continues throughout, so this is not a pause |

**Back up `lapsecoin_key.json.privacy` along with your main key file.** It's a second real keypair, created next to the main one at first start and encrypted with the same passphrase (independently, so it opens on its own). It's what the privacy setting advertises, and since peers pay advertised addresses, it can hold funds. Losing it loses those funds.
</details>

<details>
<summary>All CLI options</summary>

| Option | Default | Description |
|---|---|---|
| `--host` | `0.0.0.0` | Interface to bind for the public port |
| `--port` | `8333` | Public port for HTTP API and peer connections |
| `--private-port` | `port+2` | Private port for wallet UI, always bound to 127.0.0.1 |
| `--keyfile` | `lapsecoin_key.json` | Path to encrypted keypair |
| `--db` | `lapsecoin_chain.db` | Path to SQLite chain database |
| `--peer host:port` | - | Bootstrap peer (repeatable) |
| `--max-peers` | `125` | Hard cap on peer table size |
| `--log-level` | `INFO` | Verbosity: DEBUG, INFO, WARNING, ERROR. DEBUG adds the HTTP access log |
| `--no-gui` | off | Headless. Implied when `LAPSECOIN_PASSPHRASE` is set |
| `--no-update-check` | off | Don't check for new releases |
| `--update-check-url` | *(project)* | Where to look for the current version |
| `--releases-url` | *(project)* | Where the update notice points people |
</details>

<details>
<summary>Requirements</summary>

- Python 3.11+
- chiavdf (VDF computation and verification)
- liboqs-python (FALCON-512 signatures)
- See `requirements.txt` for the full list
</details>

## Exchanges

No LAPSE exchange listings yet.

In the meantime, [Discord](https://discord.gg/FP2d8JmK6r) hosts direct trades: 1 LAPSE for 1 SATOX. SATOX (Satoxcoin) is listed on the exchanges linked from the [satoxcoin repo](https://github.com/satoverse/satoxcoin).

---

<div align="center" id="support">

**Support the project:** BTC `bc1q8qxvr5zuws78650wz9rgzpqxfx7dqzl38rdtsw`

</div>
