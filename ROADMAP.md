# Roadmap

No dates. Items move between Now, Next and Later as the project does.
Each one links to its issue, so you can pick one up.

## The loop

```
   Build  ->  Ship  ->  Show  ->  Invite
     ^                              |
     +------------------------------+
```

- **Build**: contributors take [good first issues](https://github.com/Lapsecoin/core/labels/good%20first%20issue) and [help wanted](https://github.com/Lapsecoin/core/labels/help%20wanted) work.
- **Ship**: merged work goes out in the next release, and the release notes name who did it.
- **Show**: publish the numbers that say if the network is alive (nodes online, distinct builders, fee requests served, LAPSE burned).
- **Invite**: the README, the Discord and fresh labeled issues bring in the next contributors. They go back to Build.

## Now: easy to join, easy to run

- [ ] Tests on every PR ([#8](https://github.com/Lapsecoin/core/issues/8))
- [ ] Map of `src/` ([#9](https://github.com/Lapsecoin/core/issues/9))
- [ ] Test the untested modules ([#4](https://github.com/Lapsecoin/core/issues/4), [#5](https://github.com/Lapsecoin/core/issues/5), [#6](https://github.com/Lapsecoin/core/issues/6), [#7](https://github.com/Lapsecoin/core/issues/7))
- [ ] Linting ([#10](https://github.com/Lapsecoin/core/issues/10))

## Next: a network that more people can take part in

- [ ] Pools, so small machines can build blocks ([#3](https://github.com/Lapsecoin/core/issues/3))
- [ ] An answer to why a node would serve fee requests ([#11](https://github.com/Lapsecoin/core/issues/11))
- [ ] Publish the "Show" numbers above

## Later

- Wider real use of LAPSE, starting with fee requests.
- Exchange listing. It follows from the above, so it has no date. Exchanges generally look for active development, a stable network, real use and liquidity, so those are what we track.

## Honest limits

Consensus is longest-chain, so a majority attacker is not beaten (see the [whitepaper](docs/whitepaper.md), section 7). More independent builders is the defense, which is why pools come before listing.
