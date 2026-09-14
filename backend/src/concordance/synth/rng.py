"""Deterministic random streams.

Every part of the generator draws from its own named stream derived from the
master seed, so adding a corruption family - or changing how many draws one
family makes - cannot shift the numbers every other family sees. Without this,
"reproducible under a seed" holds only until the next code change.
"""

from __future__ import annotations

import hashlib

import numpy as np


def stream_id(*labels: str) -> int:
    """Stable 63-bit id for a stream name. Stable across processes and runs."""
    digest = hashlib.blake2b("/".join(labels).encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") >> 1


def stream(seed: int, *labels: str) -> np.random.Generator:
    """An independent generator for ``labels`` under the master ``seed``."""
    return np.random.default_rng([seed, stream_id(*labels)])


def choice(rng: np.random.Generator, values: list[str]) -> str:
    """One uniform draw from ``values``."""
    return values[int(rng.integers(0, len(values)))]


def cumulative(probs: list[float]) -> np.ndarray:
    """Cumulative distribution for inverse-CDF sampling."""
    cum = np.cumsum(np.asarray(probs, dtype=float))
    normalized: np.ndarray = cum / cum[-1]
    return normalized


def pick(rng: np.random.Generator, values: list[str], cum: np.ndarray) -> str:
    """One weighted draw by inverse CDF.

    ``rng.choice(..., p=probs)`` renormalizes the whole probability vector on
    every call, which costs more than the rest of a record put together at 50k
    rows. One uniform and a binary search does the same job.
    """
    return values[int(np.searchsorted(cum, rng.random(), side="right"))]
