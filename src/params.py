"""Protocol constants. No logic, no I/O."""

# Denomination: 1 LAPSE = 100_000_000 ticks (same precision as BTC/satoshis).
# All balances, amounts, fees, and rewards are integers in ticks.
TICKS_PER_LAPSE = 100_000_000

# Emission. Supply is bounded at 21M LAPSE with smooth exponential decay
# over a 20-year half-life. No halvings, no supply shock.
SUPPLY_CAP        = 21_000_000 * TICKS_PER_LAPSE
EMISSION_HALFLIFE = 5_000_000  # blocks (~20 years at 2 min/block)

# Per-block decay fraction (1 - 0.5**(1/EMISSION_HALFLIFE)), as an exact
# integer ratio rather than a float. Every node must derive byte-identical
# block rewards or they silently fork on replay; float exponentiation
# (`0.5 ** (1/N)`) depends on the platform's libm and isn't guaranteed
# bit-identical across interpreters/OSes/CPUs, which a plain integer ratio
# is immune to. Computed once (see EMISSION_DECAY_NUMERATOR's derivation
# below) with 60-digit decimal precision, far more precision than a
# double carries, then fixed as constants; never recomputed at runtime.
#   from decimal import Decimal, getcontext
#   getcontext().prec = 60
#   one_minus = 1 - (Decimal(1) / 2) ** (Decimal(1) / EMISSION_HALFLIFE)
#   EMISSION_DECAY_NUMERATOR = int((one_minus * EMISSION_DECAY_DENOMINATOR)
#                                  .to_integral_value())
EMISSION_DECAY_DENOMINATOR = 10 ** 40
EMISSION_DECAY_NUMERATOR   = 1386294265029292275522718605160789

# The cadence the VDF iteration count is calibrated to hit (see
# VDF_ITERATIONS). Nothing reads this to make a decision, since the real
# pace is always measured from the chain's own timestamps rather than
# assumed; it is here to name the target that calibration aims at.
BLOCK_CYCLE_SECONDS = 120

# 10 MB hard cap, raised only by network upgrade. The UDP transport sizes
# its own chunking and decompression ceilings from this (see
# peer_udp.MAX_MESSAGE_BYTES) so the two cannot drift apart: they did, and
# the half of this limit the wire could not carry was unusable and
# silently so.
BLOCK_SIZE_LIMIT = 10_000_000

MAX_PEERS = 125

ADDRESS_WORD_COUNT = 12
WORD_BITS          = 11

# VDF iteration count targeting ~120 seconds of sequential computation on
# target testnet hardware. Calibrated from real benchmark runs: median of
# three 500k-iteration timed runs measured ~6,100,000 iterations/61s, then
# doubled to reach the full ~120s target (see commits 34c65ab, d8616f3,
# a62e4bb). Re-measure if target/mainnet hardware differs from what was
# benchmarked for the testnet.
VDF_ITERATIONS = 12_200_000  # calibrated: ~120s on target hardware

# VDF difficulty adjustment. The iteration count can only increase over time
# as hardware gets faster. Adjustment happens every VDF_ADJUST_INTERVAL blocks
# using the median real block-to-block timestamp delta across that window
# (not a self-reported figure. Every node computes this identically from
# chain data alone). If median < VDF_ADJUST_MIN_SECONDS, iterations increase
# by VDF_ADJUST_FACTOR. Iterations never decrease; faster hardware means
# shorter block times until the next upward adjustment, never a security
# regression.
#
# Window size matches Bitcoin's actual real-time retarget window (2 weeks),
# not its block count. Block count alone isn't the right basis, since
# what resists manipulation is how long an attacker must sustain outsized
# influence over the window's median, not how many blocks it spans. At our
# 2-minute cadence that's 2 weeks / 2 min = 10,080 blocks.
VDF_ADJUST_INTERVAL    = 10_080  # blocks between adjustments (~2 weeks)
VDF_ADJUST_MIN_SECONDS = 100    # trigger increase if median falls below this

# Max 2% increase per adjustment period, as an exact integer ratio for the
# same reason the emission decay above is one: this is a consensus value,
# every node must derive it identically, and a float is the wrong tool for
# that even where it currently happens to agree.
#
# It did agree, and would have for a long time. int(x * 1.02) matches
# x * 51 // 50 for every starting value up to three million, and along the
# real ratcheting sequence from VDF_ITERATIONS the two stay identical for
# 882 consecutive adjustments, roughly 34 years. They part company only
# once the iteration count passes 2**53, where a double stops representing
# integers exactly, and then only by 1. So this fixes nothing that is
# broken today; it removes the last float from consensus arithmetic and
# keeps the number exact past the point where the old form quietly
# stops being.
VDF_ADJUST_NUMERATOR   = 51
VDF_ADJUST_DENOMINATOR = 50

# Genesis message. Embedded in block 0 and hashed into the genesis block hash.
# Cannot change after launch without breaking network identity.
GENESIS_MESSAGE = (
    "LapseCoin genesis. No premine. No authority. Every node earns. "
    "The chain is its own clock: one VDF per block, real elapsed time."
)

DB_PATH = "lapsecoin_chain.db"

# Two rules about a block's timestamp, which used to share one constant
# because they happen to want the same number, not because they are the
# same quantity. They are not, and the sharing hid a real effect.
#
# How far ahead of the validator's own clock a block may claim to be.
# This is clock-skew tolerance: machines disagree about the time, and a
# block should not be rejected for arriving from one that is a few seconds
# fast. Raising it forgives sloppier clocks.
TIMESTAMP_SKEW_SECONDS = 30

# How far after its parent a block's timestamp must be. This is not about
# clocks at all; it stops a builder backdating or stuffing timestamps to
# manipulate the retarget window, which is derived from exactly these
# deltas (see block.get_vdf_iterations).
#
# It is also a hard floor on block time. Blocks target ~120s, so a builder
# whose hardware is four times the calibration target would finish in 30s
# and have every node reject the result as too soon after its parent,
# while the retarget only ratchets 2% per two weeks and so would take
# years to absorb the difference. Nothing breaks (the fast builder simply
# loses the height), but it is a ceiling on how fast this chain can ever
# run, and while it shared a name with the skew tolerance it was a ceiling
# nobody was looking at. Whether 30 is the right number for it is now a
# question that can be asked on its own terms.
MIN_BLOCK_SPACING_SECONDS = 30

# Genesis timestamp: unix time when the chain was launched. Set once manually
# before the first release and never changed.
GENESIS_TIMESTAMP = 1787869281

# Number of BEP44 DHT slots used for peer discovery.
BEP44_SLOT_COUNT = 256

# TESTNET = True: GitHub Actions updates GENESIS_TIMESTAMP on every release,
# letting the chain restart fresh. Set to False for mainnet; at that point
# GENESIS_TIMESTAMP is fixed manually once and the workflow never touches it.
TESTNET      = True
NETWORK_NAME = "LapseCoin Testnet" if TESTNET else "LapseCoin"
