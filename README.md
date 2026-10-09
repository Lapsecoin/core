<div align="center">
  <img src="lapsecoin.svg" width="120" alt="LapseCoin logo" />

  # LapseCoin

  Peer-to-peer electronic cash, secured by a Verifiable Delay Function instead of proof-of-work mining, with quantum-resistant signatures.

  [![Release](https://img.shields.io/github/v/release/Lapsecoin/core)](https://github.com/Lapsecoin/core/releases)
  [![Live node](https://img.shields.io/badge/node-lapsenode.vicnas.me-2ea44f)](https://lapsenode.vicnas.me/)
  [![Whitepaper](https://img.shields.io/badge/docs-whitepaper-blue)](docs/whitepaper.md)
  [![Donate BTC](https://img.shields.io/badge/donate-BTC-f7931a)](#support)
  [![Discord](https://img.shields.io/badge/discord-join-5865F2?logo=discord&logoColor=white)](https://discord.gg/FP2d8JmK6r)
</div>

**Recommended: use a pre-built release.** Building from source requires native libraries (liboqs, chiavdf) that involve complex C/C++ compilation and can produce DLL or shared library errors depending on your platform. The release binaries on the [releases page](https://github.com/Lapsecoin/core/releases) are self-contained and require no dependencies.

Join the [Discord](https://discord.gg/FP2d8JmK6r) for discussions, news, and trades with other coins.

## Quick start

Grab a binary from the [releases page](https://github.com/Lapsecoin/core/releases), self-contained with no dependencies.

```
# Linux                    # Windows
chmod +x lapsecoin         lapsecoin.exe
./lapsecoin
```

You'll be prompted for a signing passphrase, then the wallet is at `http://localhost:8335` and the block explorer at `http://localhost:8333`.

For headless environments (Docker/systemd/CI, no GUI), set the passphrase non-interactively and pass `--no-gui`:

```bash
export LAPSECOIN_PASSPHRASE="your passphrase"
./lapsecoin --no-gui
```

## Light client (lapsecoin-dumb)

A wallet and the board with no chain, for a small data allowance or a machine that cannot keep a node running. It signs on your machine and asks a node for everything else. Run `./lapsecoin-dumb` (or `lapsecoin-dumb.exe`), enter your passphrase, and open `http://127.0.0.1:8335/`. A full node's key works as is.

It sends your address over Tor if Tor is running, else through a one-hop relay between two nodes, else directly. Nothing is ever refused for lack of a route.

<details>
<summary>How consensus works</summary>

Most cumulative proven work wins, Bitcoin-style. Two blocks at the same height carry equal work, so that tie goes to the lower VDF output, not to whichever arrived first. A height stays open to a better sibling for a few seconds (`LAPSECOIN_DRAW_WINDOW_SECONDS`) so the comparison can happen; work on the next height never stops meanwhile.

Block timing is enforced by a VDF anchored to real elapsed time, believed to have a much smaller hardware-advantage gap than proof-of-work. Transactions are ordinary and plaintext, with sender-bid fees, much like Bitcoin's own. Signatures are FALCON-512 (quantum-resistant). Full spec in [docs/whitepaper.md](docs/whitepaper.md).
</details>

<details>
<summary>Running from source</summary>

Requires Python 3.11+.

**Linux/macOS:**

```bash
curl -fsSL https://raw.githubusercontent.com/Lapsecoin/core/main/scripts/install.sh | bash
```

**Windows (PowerShell):**

```powershell
irm https://raw.githubusercontent.com/Lapsecoin/core/main/scripts/install.ps1 | iex
```

Then: `lapsecoin`. Installs build tools if missing, then the app; falls back to skipping `libtorrent` (with a starter peers list) only if that's what fails.
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

The log level can be set the same way:

```bash
export LAPSECOIN_LOG_LEVEL=DEBUG
python main.py
```

`--log-level` still wins if you pass it.
</details>

<details>
<summary>Settings and environment variables</summary>

Node-local settings live on the private wallet UI under **Settings**, and each can also be set by environment variable. The environment wins, so a container or unit file can force a value for one launch without overwriting what's saved; a value set that way shows on the page as forced rather than editable.

| Variable | Default | Meaning |
|---|---|---|
| `LAPSECOIN_PASSPHRASE` | prompted | Non-interactive wallet passphrase; also implies `--no-gui` |
| `LAPSECOIN_PORT` | `8333` | Default public HTTP and peer port; `--port` wins when supplied |
| `LAPSECOIN_LOG_LEVEL` | `INFO` | Default log level: `DEBUG`, `INFO`, `WARNING`, or `ERROR` |
| `LAPSECOIN_DOCKER` | unset | Set to `1` inside the project Docker image; disables self-update |
| `LAPSECOIN_PEERS_URL` | project default | Starter peer-list URL used by the installer and update fallback |
| `LAPSECOIN_DRAW_WINDOW_SECONDS` | `10` | Same-height draw window in seconds |
| `LAPSECOIN_SHOW_HARDWARE_DETAILS` | `true` | Show this node's hardware details on the odds page |
| `LAPSECOIN_MINING_ENABLED` | `true` | Enable this node's block building |
| `LAPSECOIN_HIDE_ADDRESS_PUBLICLY` | `true` | Hide this node's address from the public dashboard |
| `LAPSECOIN_GAS_ENABLED` | `false` | Answer fee requests from this node's ETH on Base (see Fee requests) |
| `LAPSECOIN_BASE_RPC_URL` | public Base endpoint | Base RPC the gas wallet reads and sends through |
| `LAPSECOIN_RELAY_API_KEY` | empty | Optional Relay API key; Relay is moving to requiring one |

`APPIMAGE` is set by the AppImage runtime itself and is not normally configured by users. `LAPSECOIN_REPO_RAW` is used only by the source installer to override where it downloads `requirements.txt` from.

</details>

<details>
<summary>All CLI options</summary>

| Option | Default | Description |
|---|---|---|
| `--host` | `0.0.0.0` | Interface to bind for the public port |
| `--port` | `8333` | Public port for HTTP API and peer connections |
| `--private-port` | `port+2` | Private port for wallet UI, always bound to 127.0.0.1 |
| `--key` | `$LAPSECOIN_KEY`, else `lapsecoin_key.json` | The key: a path, or the text `--export` prints (`--keyfile` still works) |
| `--export` | - | Print the key as one line of text (still encrypted) and exit |
| `--db` | `lapsecoin_chain.db` | Path to SQLite chain database |
| `--peer host:port` | - | Bootstrap peer (repeatable) |
| `--max-peers` | `125` | Hard cap on peer table size |
| `--log-level` | `INFO` (or `LAPSECOIN_LOG_LEVEL`) | Verbosity: DEBUG, INFO, WARNING, ERROR. DEBUG adds the HTTP access log |
| `--no-gui` | off | Headless. Implied when `LAPSECOIN_PASSPHRASE` is set |
| `--no-update-check` | off | Don't check for new releases |
| `--update-check-url` | *(project)* | Where to look for the current version |
| `--releases-url` | *(project)* | Where the update notice points people |
</details>

## Fee requests

Holding a token on a chain where you have none of its gas coin? Open **Fees**, pick the network and what you want to do, sign a message with the wallet that holds the address, and burn 10 LAPSE. A node operator sends you the gas. The 10 LAPSE is returned only if no node offers; otherwise it stays burned, whether or not the gas arrives, and burned LAPSE is paid out again by emission. It is a free gift from operators, as is.

To serve requests, fund the Base address shown on **Send** (a node needs about $2 of ETH) and switch on **Pay fee requests** in Settings. A request never costs a node more than $2.

## Exchanges

No LAPSE exchange listings yet.

In the meantime, the [Discord](https://discord.gg/FP2d8JmK6r) is the place to trade directly with other members.
---

<div align="center" id="support">

**Support the project:** BTC `bc1q8qxvr5zuws78650wz9rgzpqxfx7dqzl38rdtsw`

</div>
