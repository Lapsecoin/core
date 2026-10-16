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

**Recommended: use a pre-built release.** Building from source requires native libraries (liboqs, chiavdf) that involve complex C/C++ compilation and can produce DLL or shared library errors depending on your platform. The release binaries on the [downloads page](https://lapsecoin.github.io/downloads.html) are self-contained and require no dependencies.

Join the [Discord](https://discord.gg/FP2d8JmK6r) for discussions, news, and trades with other coins.

## Quick start

Grab a binary from the [downloads page](https://lapsecoin.github.io/downloads.html), self-contained with no dependencies.

```
# Linux                    # Windows
chmod +x lapsecoin         lapsecoin.msi
./lapsecoin
```

You'll be prompted for a signing passphrase, then the wallet is at `http://localhost:8335` and the block explorer at `http://localhost:8333`.

For headless environments (Docker/systemd/CI, no GUI), set the passphrase non-interactively and pass `--no-gui`:

```bash
export LAPSECOIN_PASSPHRASE="your passphrase"
./lapsecoin --no-gui
```

## Your wallet

The first start creates a wallet and shows its **recovery phrase** (12 words) once, in the window or the terminal. Write it down: it restores every address of the wallet on any machine. It is kept in `lapsecoin_wallet.json`, encrypted under your passphrase.

- **More addresses.** The **Addresses** page adds one, names them and shows each balance (`--new-address` does the same from the terminal). Each address is its own account. The node mines and builds as the one it started with (`--account N`, default 0; a name you gave the address works too); Send and the board let you pick which address signs, and remember the last one you used.
- **A key for another machine.** On a box that should mine without holding your recovery phrase, run it with one address's key only: **Addresses > Export a key**, or `./lapsecoin --export --account 1` (or `--account shop`). It prints about 120 characters (`lc1-...`) sealed under a *new* passphrase. On that machine use it as `--key` or `$LAPSECOIN_KEY` (with `LAPSECOIN_PASSPHRASE`). It never holds the phrase, so losing the machine exposes that one address and nothing else. Nothing is written to disk there.
- **Restoring.** In an empty folder, `./lapsecoin --restore` asks for the phrase (hidden input, or piped in) and a passphrase. When the node has synced it finds the other addresses the phrase has used.
- **Upgrading an older node.** A node that still has `lapsecoin_key.json` moves to a recovery phrase at its next start: it creates the wallet under the same passphrase, shows the phrase once and runs as the new address. Once synced it sends the old address's funds to it in one ordinary transaction and deletes the old key. Without a terminal or window (Docker, systemd) the phrase is printed once to the program's output, never to the log file.

The phrase follows the convention on the [wallet integration page](https://lapsecoin.github.io/wallets.html), so other wallets that follow it restore the same addresses. FALCON key generation from a seed is not in liboqs, so a small library built from the same PQClean sources (`native/falcon_seeded`) provides it; the install scripts and releases include it, and a source checkout builds it with `make -C native/falcon_seeded`.

## Fee requests

**Fees** in the wallet gets you gas on another chain: pick the network and what you want to do, sign with the wallet that holds the address, and burn 10 LAPSE (returned only if no node offers). To serve requests, fund the Base address on **Send**. A node helps while that wallet holds enough, and does nothing otherwise.

## Exchanges

No LAPSE exchange listings yet.

In the meantime, the [Discord](https://discord.gg/FP2d8JmK6r) is the place to trade directly with other members.
---

<div align="center" id="support">

**Support the project:** BTC `bc1q8qxvr5zuws78650wz9rgzpqxfx7dqzl38rdtsw`

</div>
