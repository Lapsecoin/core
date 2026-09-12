# LapseCoin: A Peer-to-Peer Electronic Cash System

*Victorio Nascimento*

## Abstract

LapseCoin keeps Bitcoin's consensus model: the chain with the most proven work wins. Proof-of-work is replaced with a Verifiable Delay Function (VDF), tying block production to real elapsed time rather than a race for a lucky hash. Transactions are ordinary and plaintext, with sender-bid fees, much like Bitcoin's own. The VDF is believed to have a much smaller hardware advantage gap than proof-of-work, so it does not push the network toward the same resource-consumption spiral.

## 1. Consensus: Verifiable Delay Functions

Each block requires a VDF proof computed over the hash of the previous block and the builder's address. The VDF takes about 120 seconds of strictly sequential computation, and no amount of parallel hardware speeds it up. Binding the builder's address into the challenge stops anyone from copying a broadcast proof and claiming it under their own address: each builder evaluates a different VDF, so a stolen proof verifies against nobody else's challenge.

The transaction list is not part of the VDF challenge. A block rejected for a transaction problem can be fixed and rebroadcast without redoing the 120 seconds of work.

When two chains compete, the one with more cumulative proven VDF iterations wins, not the one with more blocks. An iteration count only counts if the proof verifies for that many, so it can't be inflated by lying.

Ties are routine rather than rare: two builders at the same height do the same protocol-set iteration count, so a plain fork ties exactly. Ties break on the lower VDF output, never on the block hash. The block hash covers the transaction list, which is not part of the VDF challenge, so a builder could swap transactions for free after the real work and grind hash variants at nearly zero cost. The VDF output can't be moved without redoing the 120 seconds, which keeps the tie-break's cost real. A height stays open to a lower-output block for a short window after one is adopted, otherwise whatever arrived first wins as soon as anything is built on it and the tie-break decides nothing. Work on the next height continues throughout.

Rewriting old history means redoing every VDF since that point, sequentially, in as much real time as the honest chain took to produce them. The honest chain keeps advancing the whole time, so the gap only grows.

## 2. Why a VDF instead of proof-of-work

**Proof-of-work has no ceiling; a VDF has a floor instead.** Hash rate buys share without limit, which is what took Bitcoin from CPUs to ASICs and a growing energy bill. A VDF challenge is one sequential computation, about 120 seconds here, that no amount of hardware shrinks below. Building a block is not about running more attempts in parallel but about how fast a single chain of steps evaluates, and the arithmetic sets a hard floor on that.

**The floor still leaves a hardware gap, just a bounded one.** Chia Network's 2019 competition on the same class-group VDF construction found specialized implementations beating commodity software by roughly 3 to 10 times. Unlike ASICs, which widened the lead every generation, that gap caps it. Below the top band a builder does not win a smaller proportional share the way lower hash rate does: it loses outright to any faster builder, having never finished in time to be compared.

**Inside the top band it is a lottery, by design.** Builders that finish close together tie, and the tie goes to the lowest VDF output, a value fixed by the previous hash and the builder's address but indistinguishable from random across addresses. Each address completing a full evaluation gets exactly one draw.

Nothing limits how many addresses an operator runs, and each needs its own real evaluation with no way to grind for a better one, so N addresses at the top tier win exactly N times the single-address share. Splitting one machine across several addresses does worse, since each fragment then runs too slow to compete. Both results were checked against a Monte Carlo model.

**That draw is where Sybil resistance is priced, not a coin fee.** An extra draw costs a full VDF of real machine-time, so participation and its energy scale linearly with spending, as Bitcoin's hash rate does, but without the arms race: a capped hardware gap means spending more mostly buys more whole machines. A coin-denominated registration fee was rejected because the economics track hardware and machine count, not balance, and a fee payable only from a balance would lock out the empty-handed new node this design means to admit.

**One Sybil surface sits outside this analysis: eclipse attacks on peer discovery**, where a node's view of the network is crowded out by attacker-controlled addresses. That is handled by capping how many peers from one address subnet a node admits, not by anything priced in coin.

## 3. Transactions

The base unit is the tick. One LAPSE equals 100,000,000 ticks.

A transaction is a plain, visible dict: a sender address, a public key, a list of outputs (recipient and amount), a sequential per-sender nonce, a fee, and a signature, plus an optional short plaintext memo. Nothing about it is encrypted or hidden, the memo included: it is a public note, not a private message.

Nonces are sequential per sender, starting from zero: a transaction's nonce must be exactly one more than the sender's last confirmed nonce. This is the standard replay-protection scheme, the same one Bitcoin-style account models use.

Fees are chosen by the sender, not fixed by the protocol. A transaction is valid as long as the sender's balance covers every output plus the fee. Builders are free to prioritize whichever pending transactions pay the most per byte, the same market-based mechanism Bitcoin uses to clear its mempool under load.

Blocks apply their listed transactions in order, checking each one against the state as it stands after the transactions before it in the same block. There is no required canonical ordering across transactions; a block's builder can list them however it likes, as long as each one is individually valid at the point it is applied.

## 4. Fees and block rewards

The builder receives the full block reward for every block, unconditionally, plus every transaction fee in that block. There is no split, and no separate party to pay out to.

## 5. Supply

```
reward(block) = floor((21,000,000 LAPSE - total minted) * (1 - 0.5^(1/5,000,000)))
```

The halflife is about 5,000,000 blocks, roughly 20 years at 2 minutes per block. This smooth curve avoids the instability a hard halving schedule can cause.

## 6. Privacy and networking

Transactions and blocks both propagate through Dandelion routing, so no observer can reliably tell which peer first broadcast a given item. A block's builder address is public by construction, since that is who gets paid, but which machine produced it need not be. The originator re-sends anything that never comes back, so handing an item to one peer is not a gamble.

A node tells its peers nothing about which address it holds. It used to, so that peers could pay it, and that made every peer table a directory mapping an address to the machine that reported it. Where to pay a node now travels as a small note relayed on the same path as everything else, so the peer handing it over is almost never its author and no address is attributable to a machine.

Signatures use FALCON-512, a lattice-based scheme designed to resist quantum computers. Addresses are twelve-word phrases derived from the public key.

Peers find each other through the BitTorrent DHT. A node only connects to peers sharing its genesis block hash. The full chain is kept forever, so balances can always be recomputed from scratch.

## 7. Security and censorship: what this design does and does not solve

Consensus here is longest-chain, exactly like Bitcoin's, just measured in proven VDF iterations instead of hashes. That inherits Bitcoin's security model in full, including its limits.

Ordinary transaction censorship, a single non-majority actor refusing to include some transaction, is defeated the same way it always has been: any other willing participant can include it instead, and ordinary confirmation-depth economics protect against a brief refusal turning into a permanent one.

A sustained majority attacker is a different matter, and this design does not claim to beat Bitcoin there. Fork choice sees only cumulative proven work, never what a chain contains, so a majority attacker can fork from before a transaction confirmed and build a history that never confirms it, at the cost of an ordinary reorg. No block-level or transaction-level rule stops that, because the attacker breaks no rule; it simply declines to extend the branch it dislikes. That is a property of longest-chain consensus generally, not a gap specific to this design.

Other inherited limitations:

- As with Bitcoin, a node syncing from scratch cannot cryptographically distinguish the honest chain from an attacker's alternative on its own; it has to trust the network it connects to at least once.
- Whether VDF-solving hardware availability keeps pace with the network's needs is a market question the protocol cannot guarantee, the same as mining hardware availability is for Bitcoin.

## 8. Conclusion

LapseCoin keeps Bitcoin's core guarantee: no trust required, everything verifiable, no authority can reverse a transaction. It replaces proof-of-work with a Verifiable Delay Function, which is believed to narrow the hardware-advantage gap that drove proof-of-work's runaway energy use, without needing a separate rule bolted on to achieve that. Transactions stay ordinary and plaintext, with sender-bid fees. Supply is capped at 21 million LAPSE with smooth decay and no halvings. Censorship resistance beyond ordinary confirmation-depth security is not claimed, because no rule at the block or transaction level can give it against a genuine majority attacker in a longest-chain system.

## References

1. S. Nakamoto, "Bitcoin: A Peer-to-Peer Electronic Cash System," 2008.
2. NIST, "FIPS 206 (Draft): FN-DSA (FALCON)," 2025.
3. G. Fanti et al., "Dandelion: Redesigning the Bitcoin Network for Anonymity," 2018.
4. A. Loewenstern et al., "BEP 44: Storing arbitrary data in the DHT," 2014.
5. D. Boneh et al., "Verifiable Delay Functions," 2018.
6. Chia Network, "Chia Network's Proof of Space and Time VDF Competition Results," 2019.
