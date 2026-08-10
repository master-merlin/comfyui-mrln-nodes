"""FROZEN deterministic seeding. Changing anything here silently reshuffles
every seed users have stored — treat as immutable API.

derive_rng: SHA-256 of "seed:slot_key" -> 64-bit -> random.Random (Mersenne
Twister; stream is stable across CPython versions and platforms). Builtin
hash() is process-salted and must never be used here.

Draw contract per slot: exactly one weighted_index() call when the slot is
random, then wildcard draws in document order of the chosen text. Fixed
selections consume no draw.
"""

import hashlib
import random


def derive_rng(seed, slot_key):
    digest = hashlib.sha256(f"{seed}:{slot_key}".encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def weighted_index(rng, weights):
    """Pick an index by weight using exactly ONE rng.random() call."""
    total = sum(weights)
    if total <= 0:
        return 0
    x = rng.random() * total
    acc = 0.0
    for i, w in enumerate(weights):
        acc += w
        if x < acc:
            return i
    return len(weights) - 1
