"""VDF (Verifiable Delay Function) wrapper around chiavdf.

Public interface:
  evaluate(challenge, iterations) -> (output_hex, proof_hex, elapsed_seconds)
  verify(challenge, output, proof, iterations) -> bool

evaluate() blocks for ~BLOCK_CYCLE_SECONDS. verify() returns in milliseconds.

The iteration count is now a protocol parameter that adjusts upward over time
as hardware improves (see block.get_vdf_iterations). It is passed explicitly
so all nodes agree on the same value for each block height.

Internals
---------
chiavdf uses a Wesolowski VDF over imaginary class groups. The discriminant
is derived fresh from each challenge so no trusted setup is needed. All class
group elements are serialized in chiavdf's compressed BQFC format, which is
exactly BQFC_FORM_SIZE=100 bytes for a 1024-bit discriminant.

prove() signature (from fastvdf.cpp):
  prove(challenge, x, disc_size_bits, num_iterations, shutdown_file) -> bytes
  Returns SerializeForm(y) + SerializeForm(proof) = 200 bytes.

verify_n_wesolowski() signature:
  verify_n_wesolowski(discriminant, x, proof_blob, num_iterations,
                      disc_size_bits, recursion) -> bool
  recursion must be 0 for a standard single Wesolowski proof.

Calibration
-----------
VDF_ITERATIONS in params.py must be measured on target hardware before genesis.
Use at least 500_000 iterations (~15 s) so OS scheduling jitter doesn't dominate.
Run 3 times and take the median:

  import chiavdf, hashlib, time
  N          = 500_000
  challenge  = hashlib.sha256(b"lapsecoin_genesis").digest()
  initial_el = bytes([0x08]) + bytes(99)
  t0         = time.monotonic()
  chiavdf.prove(challenge, initial_el, 1024, N, "")
  elapsed    = time.monotonic() - t0
  print(f"elapsed {elapsed:.2f}s  VDF_ITERATIONS = {int(120 * N / elapsed)}")
"""

import os
import tempfile
import time

import chiavdf

from params import VDF_ITERATIONS

DISC_SIZE_BITS = 1024
FORM_SIZE      = 100
N_WESOLOWSKI   = 0

# The identity element (BQFC form, first byte 0x04) must never be the starting
# element: it is a fixed point, identity^(2^T) = identity, so every block's
# VDF output would be identical.

# Generator element: first byte 0x08, remaining bytes 0x00.
# This is a valid non-identity BQFC form that produces unique VDF output
# per challenge. Verified empirically: chiavdf.prove(challenge, _GENERATOR, ...)
# produces a non-identity, challenge-dependent output.
_GENERATOR = bytes([0x08]) + bytes(FORM_SIZE - 1)


class Cancelled(Exception):
    """Raised by evaluate() when cancel() was called on its handle before
    chiavdf.prove() returned. chiavdf has no Python-level interrupt for a
    proof already in flight; the shutdown_file argument is its own
    mechanism for this (prove() polls for the file's removal and aborts
    early). We still can't distinguish "aborted early" from "would have
    returned normally around now" from the return value alone, so a
    caller that cancelled must not trust whatever prove() returns, only
    that it's safe to stop waiting on this call."""


class EvaluationHandle:
    """Lets a caller cancel an in-flight evaluate() from another thread.

    Backed by chiavdf's shutdown_file convention: prove() is created a
    fresh file to watch, and periodically checks whether it still exists;
    deleting it is prove()'s own signal to abort early. Verified directly
    against the installed chiavdf build (not assumed): a 50M-iteration
    prove() call, deleted-file-triggered, returned in ~1.1s with an
    incomplete result instead of running to completion. evaluate() never
    trusts that returned payload when cancelled either way. It always
    raises Cancelled off handle._cancelled, not off inspecting the return
    value, since an aborted call's result isn't a real proof regardless of
    what bytes happened to come back.
    """

    def __init__(self):
        fd, self._path = tempfile.mkstemp(prefix="lapsecoin_vdf_shutdown_")
        os.close(fd)
        self._cancelled = False

    @property
    def path(self):
        return self._path

    def cancel(self):
        """Signal chiavdf to abort this evaluation early. Idempotent."""
        self._cancelled = True
        try:
            os.remove(self._path)
        except FileNotFoundError:
            pass

    def _cleanup(self):
        try:
            os.remove(self._path)
        except FileNotFoundError:
            pass


def evaluate(challenge: bytes,
             iterations: int = VDF_ITERATIONS,
             handle: "EvaluationHandle | None" = None) -> tuple[str, str, float]:
    """Run the VDF. Blocks for approximately BLOCK_CYCLE_SECONDS.

    challenge:  32 bytes from block.vdf_challenge(previous_hash, builder).
    iterations: number of sequential squarings. Pass block.get_vdf_iterations(chain).
    handle: an EvaluationHandle whose cancel() can be called from another
        thread to abort this call early (see EvaluationHandle). Optional;
        omit for the old uncancellable behavior (shutdown_file="").
    Returns (output_hex, proof_hex, elapsed_seconds).
    Raises Cancelled if handle.cancel() was called before prove() returned.
    """
    t0 = time.monotonic()
    try:
        result = chiavdf.prove(
            challenge,
            _GENERATOR,
            DISC_SIZE_BITS,
            iterations,
            handle.path if handle is not None else "",
        )
    finally:
        if handle is not None:
            handle._cleanup()
    if handle is not None and handle._cancelled:
        raise Cancelled()
    elapsed = time.monotonic() - t0
    output  = result[:FORM_SIZE]
    proof   = result[FORM_SIZE:]
    return output.hex(), proof.hex(), elapsed


def verify(challenge: bytes, output: str, proof: str,
           iterations: int = VDF_ITERATIONS) -> bool:
    """Verify a VDF proof. Returns in milliseconds.

    challenge:  32 bytes from block.vdf_challenge(previous_hash, builder).
    output, proof: hex strings from evaluate() stored in the block.
    iterations: must match what was used during evaluate().
    """
    try:
        disc       = chiavdf.create_discriminant(challenge, DISC_SIZE_BITS)
        proof_blob = bytes.fromhex(output) + bytes.fromhex(proof)
        return chiavdf.verify_n_wesolowski(
            disc,
            _GENERATOR,
            proof_blob,
            iterations,
            DISC_SIZE_BITS,
            N_WESOLOWSKI,
        )
    except Exception:
        return False
