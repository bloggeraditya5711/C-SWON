# C-SWON Validator — Miner Selection & Task Selection
# Implements VRF-keyed task selection (readme §2.5, §4.8 step 1)
# and early miner boost logic (readme §3.5).

"""
Deterministic task selection using a VRF-style hash of (validator_hotkey, block).
Early participation boost: first 50 miners get 3× query frequency.
"""

import hashlib
import json
import os
import random
from typing import List, Optional

import bittensor as bt
import numpy as np

from cswon.validator.config import (
    EARLY_MINER_BOOST_MULTIPLIER,
    EARLY_MINER_LIMIT,
    BENCHMARK_PATH,
)


def load_benchmark_tasks(benchmark_path: Optional[str] = None) -> List[dict]:
    """
    Load active benchmark tasks from the versioned JSON file.
    Skips tasks whose status != "active" (readme §4.7).
    """
    path = benchmark_path or BENCHMARK_PATH
    if not os.path.exists(path):
        bt.logging.warning(f"Benchmark file not found at {path}, returning empty task list")
        return []

    with open(path, "r") as f:
        all_tasks = json.load(f)

    # Filter to active tasks only (readme §4.7: validators skip tasks whose status != "active")
    active_tasks = [t for t in all_tasks if t.get("status", "active") == "active"]
    bt.logging.info(f"Loaded {len(active_tasks)} active benchmark tasks out of {len(all_tasks)} total")
    return active_tasks


def select_task_for_block(
    validator_hotkey: str,
    current_block: int,
    benchmark_tasks: List[dict],
) -> Optional[dict]:
    """
    Deterministic task selection using VRF-keyed hash (readme §2.5, §4.8 step 1).

    Different validators derive different tasks from the same block via their
    hotkey-keyed VRF. Cross-validator consensus uses distributional statistics
    over the rolling window, not identical-task point comparisons.

    Returns None if no tasks are available.
    """
    if not benchmark_tasks:
        return None

    seed = f"{validator_hotkey}:{current_block}".encode()
    h = hashlib.sha256(seed).digest()
    task_index = int.from_bytes(h, "big") % len(benchmark_tasks)
    return benchmark_tasks[task_index]


def select_miners_for_query(
    metagraph: "bt.metagraph",
    k: int = 10,
    exclude: Optional[List[int]] = None,
    registration_blocks: Optional[dict] = None,
    min_stake_tao: float = 1.0,
) -> np.ndarray:
    """
    Select miners to query with early participation boost (readme §3.5)
    and minimum active stake enforcement (readme §3.1).

    The first 50 registered miners (by registration order) get 3× query
    frequency — their selection probability is tripled. This is implemented
    by giving them 3× weight in the random sampling.

    Miners below min_stake_tao active stake are excluded, EXCEPT miners
    still within their immunity period (approximated as uid < EARLY_MINER_LIMIT)
    to avoid penalising brand-new participants before they can acquire stake.

    Args:
        metagraph: The metagraph object.
        k: Number of miners to select.
        exclude: UIDs to exclude from selection.
        registration_blocks: Optional dict mapping uid -> registration block.
        min_stake_tao: Minimum active TAO stake required (readme §3.1). Default 1.0.

    Returns:
        np.ndarray: Selected miner UIDs.
    """
    exclude = exclude or []
    n = metagraph.n.item()

    # Build candidate list with weights for early miner boost
    candidates = []
    weights = []

    for uid in range(n):
        # Skip non-serving axons
        if not metagraph.axons[uid].is_serving:
            continue
        # Skip excluded UIDs
        if uid in exclude:
            continue
        # Skip validators (those with validator permits and high stake)
        if metagraph.validator_permit[uid] and metagraph.S[uid] > 1024:
            continue

        # Minimum stake enforcement (readme §3.1).
        # Exception: early miners (uid < EARLY_MINER_LIMIT) approximate the immunity
        # period — they are included regardless of stake so new participants aren't
        # starved of queries before they can acquire the required stake.
        is_early_miner = uid < EARLY_MINER_LIMIT
        miner_stake = float(metagraph.S[uid])
        if miner_stake < min_stake_tao and not is_early_miner:
            bt.logging.trace(
                f"Skipping miner uid={uid}: stake={miner_stake:.3f} < "
                f"min_stake_tao={min_stake_tao}"
            )
            continue

        candidates.append(uid)

        # Early miner boost: first EARLY_MINER_LIMIT miners get higher selection weight
        if uid < EARLY_MINER_LIMIT:
            weights.append(float(EARLY_MINER_BOOST_MULTIPLIER))
        else:
            weights.append(1.0)

    if not candidates:
        return np.array([], dtype=int)

    # Normalise weights to probabilities
    total_weight = sum(weights)
    probabilities = [w / total_weight for w in weights]

    # Sample without replacement
    k = min(k, len(candidates))
    selected = np.random.choice(
        candidates, size=k, replace=False, p=probabilities
    )
    return selected

