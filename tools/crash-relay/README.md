# Crash relay

LapseCoin sends a sanitized report when it crashes (see `src/crashlog.py`). The
program cannot hold a GitHub token, because anyone could read it out of the
installer, so the report goes here, to a small Cloudflare Worker that holds the
token and files the issue: one issue per kind of crash, and a short comment for
every repeat.

What it receives is only what the program sends: the exception, the last
start-up steps, the version, the Windows build and the architecture, with the
account name, folder paths and LapseCoin addresses already taken out. It stores
nothing about the sender. It counts requests per day, keyed by a hash of the
sender's address, to slow abuse (5 per sender and 200 in total per day); those
keys expire after two days.

## Set it up once

1. Create a fine-grained GitHub token for `Lapsecoin/core` with only
   **Issues: read and write**.
2. In this folder: `npm install -g wrangler`, then `wrangler login`.
3. `wrangler kv namespace create LIMITS`, and paste the id it prints into
   `wrangler.toml`.
4. `wrangler secret put GITHUB_TOKEN` and paste the token.
5. `wrangler deploy`. It prints the worker's address.
6. In the `Lapsecoin/core` repository settings, add the Actions **variable**
   `CRASH_REPORT_URL` with that address. Releases built after that send
   reports; a build without it sends nothing.

Create a label named `crash-report` in the repository too.

## Check it

`node --test tools/crash-relay/worker.test.mjs` runs the tests (no network, no token).

## Turn reporting off

For one person: set `LAPSECOIN_CRASH_REPORTS=0`, or put an empty file named
`crash-reports-off` in the data folder. For everyone: leave `CRASH_REPORT_URL`
unset when building.
