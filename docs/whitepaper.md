# LapseCoin: A Peer-to-Peer Electronic Cash System

*Victorio Nascimento*

## Abstract

LapseCoin keeps Bitcoin's consensus model: the chain with the most proven work wins. Proof-of-work is replaced with a Verifiable Delay Function (VDF), tying block production to real elapsed time rather than a race for a lucky hash. Transactions are ordinary and plaintext, with sender-bid fees. The VDF is believed to have a much smaller hardware advantage gap than proof-of-work, so it does not push the network toward the same resource-consumption spiral.

## 1. Consensus: Verifiable Delay Functions

Each block requires a VDF proof computed over the hash of the previous block and the builder's address. The VDF takes about 120 seconds of strictly sequential computation, and no amount of parallel hardware speeds it up. Binding the builder's address into the challenge means a broadcast proof cannot be copied and claimed under another address.

The transaction list is not part of the challenge, so a block rejected for a transaction problem can be fixed and rebroadcast without redoing the 120 seconds.

When two chains compete, the one with more cumulative proven VDF iterations wins, not the one with more blocks. An iteration count only counts if the proof verifies for that many, so it cannot be inflated by lying. Rewriting old history means redoing every VDF since that point, sequentially, while the honest chain keeps advancing.

**Ties.** Two builders at the same height do the same protocol-set iteration count, so a plain fork ties exactly, and a tie can run many blocks deep.

- Ties break on VDF output, never on the block hash. The hash covers the transaction list, so a builder could grind hash variants at nearly zero cost, while the VDF output cannot be moved without redoing the 120 seconds.
- A contested height stays open to a lower-output block for a short window after one is adopted. Otherwise whatever arrived first would win and the tie-break would decide nothing.
- A tie spanning several heights is settled at every diverged height: the chain with the lower output at more of them wins. Undoing N blocks of tied work therefore costs an advantage sustained over all N.

## 2. Why a VDF instead of proof-of-work

**Proof-of-work has no ceiling; a VDF has a floor.** Hash rate buys share without limit, which took Bitcoin from CPUs to ASICs and a growing energy bill. A block here is one sequential computation that no hardware shrinks below its floor, so building a block is about how fast a single chain of steps evaluates, not how many attempts run in parallel.

**The floor still leaves a hardware gap, but a bounded one.** Chia Network's 2019 competition on the same class-group construction found specialized implementations beating commodity software by roughly 3 to 10 times. That gap is capped rather than widening each generation. A builder below the top band does not win a smaller share; it loses outright, having never finished in time.

**Inside the top band it is a lottery, by design.** Builders that finish close together tie, and the tie goes to the lowest VDF output, which is indistinguishable from random across addresses. Each address completing a full evaluation gets exactly one draw, with no way to grind for a better one. N addresses at the top tier win N times the single-address share, and splitting one machine across several addresses does worse, since each fragment runs too slow to compete.

**That draw is where Sybil resistance is priced, not a coin fee.** An extra draw costs a full VDF of real machine-time, so participation scales linearly with spending, as Bitcoin's hash rate does, but without the arms race: a capped hardware gap means spending more mostly buys more whole machines. A coin-denominated registration fee was rejected because it would lock out the empty-handed new node this design means to admit.

## 3. Transactions

The base unit is the tick. One LAPSE equals 100,000,000 ticks.

A transaction is plaintext and public: a sender address, a public key, a list of outputs (recipient and amount), a per-sender nonce, a fee, a signature, and an optional short memo. The memo is a public note, not a private message.

A transaction's nonce must be exactly one more than the sender's last confirmed nonce, starting from zero, which prevents replay. It is valid as long as the sender's balance covers every output plus the fee. Fees are chosen by the sender, and builders prioritize the highest payers, the same market mechanism Bitcoin uses.

A block is capped at 2 MB. Blocks apply their transactions in the listed order, each checked against the state left by the ones before it.

## 4. Fees and block rewards

The builder receives the full block reward plus every transaction fee in the block, with no split.

Posts to the public on-chain board carry a protocol-enforced minimum fee that rises in steps as more posts confirm over the board's lifetime, so they cannot be spammed at a flat cost. The floor rises slowly enough that it cannot invalidate a block's worth of already-broadcast posts at once.

## 5. Supply

```
reward(block) = floor((21,000,000 LAPSE - (total minted - burned)) * (1 - 0.5^(1/5,000,000)))
```

Burned is what the burn address holds, counted back into the pool. The halflife is about 5,000,000 blocks, roughly 20 years at 2 minutes per block. This smooth curve avoids the instability of a hard halving schedule.

## 6. Privacy and networking

Transactions and blocks propagate through Dandelion routing, so no observer can reliably tell which peer first broadcast an item. A block's builder address is public, since that is who gets paid, but the machine that produced it need not be. Nodes do not announce which address they hold.

Signatures use FALCON-512, a lattice-based scheme designed to resist quantum computers. Addresses are twelve-word phrases derived from the public key. Peers find each other through the BitTorrent DHT and only connect to peers sharing their genesis block hash. Eclipse attacks are limited by capping how many peers from one address subnet a node admits.

## 7. Security and censorship

Consensus is longest-chain, exactly like Bitcoin's, measured in proven VDF iterations instead of hashes. That inherits Bitcoin's security model in full, including its limits.

A single non-majority actor refusing to include a transaction is defeated as it always has been: any other participant can include it, and confirmation depth protects against a brief refusal becoming permanent.

A sustained majority attacker is different, and this design does not claim to beat Bitcoin there. Fork choice sees only cumulative proven work (and, on an exact tie, which side won more diverged heights). It never looks at what a chain contains, so a majority attacker can fork from before a transaction confirmed and build a history that never confirms it, at the cost of an ordinary reorg. That is a property of longest-chain consensus generally.

Other inherited limitations:

- A node syncing from scratch cannot cryptographically tell the honest chain from an attacker's alternative; it must trust the network it connects to at least once.
- Whether VDF hardware availability keeps pace with the network's needs is a market question the protocol cannot guarantee, as with mining hardware for Bitcoin.

## References


1. S. Nakamoto, "Bitcoin: A Peer-to-Peer Electronic Cash System," 2008.
2. NIST, "FIPS 206 (Draft): FN-DSA (FALCON)," 2025.
3. G. Fanti et al., "Dandelion: Redesigning the Bitcoin Network for Anonymity," 2018.
4. A. Loewenstern et al., "BEP 44: Storing arbitrary data in the DHT," 2014.
5. D. Boneh et al., "Verifiable Delay Functions," 2018.
6. Chia Network, "Chia Network's Proof of Space and Time VDF Competition Results," 2019.
