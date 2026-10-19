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

**Recommended: use a pre-built release.** Building from source requires native libraries (liboqs, chiavdf) that involve complex C/C++ compilation and can produce DLL or shared library errors depending on your platform. The release binaries are self-contained and require no dependencies.

Join the [Discord](https://discord.gg/FP2d8JmK6r) for discussions, news, and trades with other coins.

## Quick start

Grab a binary from the [downloads page](https://lapsecoin.github.io/downloads.html).

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

## Fee requests

**Fees** in the wallet gets you gas on another chain: pick the network and what you want to do, sign with the wallet that holds the address, and burn 10 LAPSE (returned only if no node offers). To serve requests, fund the Base address on **Send**. A node helps while that wallet holds enough, and does nothing otherwise.

## Exchanges

No LAPSE exchange listings yet.

In the meantime, the [Discord](https://discord.gg/FP2d8JmK6r) is the place to trade directly with other members.
## Contributing

PRs and issues welcome. See [CONTRIBUTING.md](CONTRIBUTING.md), the [roadmap](ROADMAP.md) and the [good first issues](https://github.com/Lapsecoin/core/labels/good%20first%20issue).

---

<div align="center" id="support">

**Support the project:** BTC `bc1q8qxvr5zuws78650wz9rgzpqxfx7dqzl38rdtsw`

</div>
