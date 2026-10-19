# Contributing

Thanks for helping out. Small PRs are welcome, and so are questions.

## Where to start

- Browse [good first issues](https://github.com/Lapsecoin/core/labels/good%20first%20issue) or [help wanted](https://github.com/Lapsecoin/core/labels/help%20wanted).
- Comment on an issue to claim it. No need to wait for permission.
- Stuck? Ask in the [Discord](https://discord.gg/FP2d8JmK6r) or on the issue.

## Setup

Python 3.10+.

```bash
git clone https://github.com/Lapsecoin/core && cd core
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest
make test
```

`liboqs` and `chiavdf` need native builds and can be fiddly. If setup fights you, open an issue and we'll help.

## Making a change

1. Fork, then branch off `main`.
2. Keep the PR focused. One idea per PR.
3. Add or update a test in `tests/` when behavior changes.
4. Run `make test`.
5. Open the PR and fill in the short template.

Match the style of the code around you. Docs-only and test-only PRs are great first contributions.

## Consensus changes

Changes to block format, validation, fees, or `params.py` affect every node. Open an issue to discuss first, so nobody wastes effort.

## Security

Don't file vulnerabilities as public issues. See [SECURITY.md](SECURITY.md).

## License

Contributions are under the project's [LICENSE](LICENSE).
