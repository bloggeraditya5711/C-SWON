# C-SWON Validator — Forward Pass
# Implements the six-stage evaluation pipeline (readme §4.8).

"""
Validator forward pass: the main loop that runs every step.

Six-stage pipeline:
1. Deterministic task selection (VRF-keyed)
2. Miner workflow collection (async query)
3. Sandboxed execution (executor)
4. Output quality evaluation (deterministic, no LLM judge)
5. Composite scoring (four-dimension formula)
6. Rolling window update + lifecycle tracking + N_min logging
"""

import time
from typing import Optional

import bittensor as bt
import numpy as np

from cswon.protocol import WorkflowSynapse
from cswon.validator.config import (
    SCORING_VERSION,
    QUERY_TIMEOUT_S,
    TEMPO,
    EXEC_SUPPORT_N_MIN,
)
from cswon.validator.miner_selection import (
    load_benchmark_tasks,
    select_task_for_block,
    select_miners_for_query,
)
from cswon.validator.query_loop import query_miners, validate_response
from cswon.validator.executor import execute_workflow
from cswon.validator.reward import (
    score_output_quality,
    compute_composite_score,
)
from cswon.validator.benchmark_lifecycle import BenchmarkLifecycleTracker


# ── Module-level state ──────────────────────────────────────────────────────

# Cache benchmark tasks to avoid reloading every step
_benchmark_cache = None

# Lifecycle tracker — singleton across forward() calls
_lifecycle_tracker: Optional[BenchmarkLifecycleTracker] = None

# N_min counter: tasks evaluated in the current tempo (readme §4.6)
_tasks_executed_this_tempo: int = 0

# Track the last tempo boundary we processed lifecycle/N_min logging for
_last_lifecycle_tempo: int = -1


def _get_benchmark_tasks():
    """Load and cache active benchmark tasks."""
    global _benchmark_cache
    if _benchmark_cache is None:
        _benchmark_cache = load_benchmark_tasks()
    return _benchmark_cache


def _get_lifecycle_tracker() -> BenchmarkLifecycleTracker:
    """Return the module-level lifecycle tracker, creating it if needed."""
    global _lifecycle_tracker
    if _lifecycle_tracker is None:
        _lifecycle_tracker = BenchmarkLifecycleTracker()
    return _lifecycle_tracker


# ── Forward pass ────────────────────────────────────────────────────────────

async def forward(self):
    """
    Validator forward pass — six-stage evaluation pipeline (readme §4.8).

    Args:
        self: The validator neuron instance.
    """
    global _tasks_executed_this_tempo, _last_lifecycle_tempo

    benchmark_tasks = _get_benchmark_tasks()
    tracker = _get_lifecycle_tracker()

    # ── Stage 1: Deterministic task selection ────────────────────
    if not benchmark_tasks:
        bt.logging.warning("No benchmark tasks loaded, skipping forward pass")
        time.sleep(5)
        return

    task = select_task_for_block(
        validator_hotkey=self.wallet.hotkey.ss58_address,
        current_block=self.block,
        benchmark_tasks=benchmark_tasks,
    )

    if task is None:
        bt.logging.warning("No task selected for this block, skipping")
        time.sleep(5)
        return

    task_id = task.get("task_id", "unknown")
    task_type = task.get("task_type", "unknown")
    bt.logging.info(
        f"Selected task: {task_id} type={task_type} at block {self.block}"
    )

    # ── Stage 2: Miner workflow collection ──────────────────────
    miner_uids = select_miners_for_query(
        metagraph=self.metagraph,
        k=self.config.neuron.sample_size,
        exclude=[self.uid],
    )

    if len(miner_uids) == 0:
        bt.logging.warning("No miners available to query")
        time.sleep(5)
        return

    # Build the WorkflowSynapse with task package
    synapse = WorkflowSynapse(
        task_id=task.get("task_id", ""),
        task_type=task_type,
        description=task.get("description", ""),
        quality_criteria=task.get("quality_criteria", {}),
        constraints=task.get("constraints", {}),
        available_tools=task.get("available_tools", {}),
        send_block=self.block,
    )

    # Query miners asynchronously with sub-block timeout (readme §4.1)
    responses = await query_miners(
        dendrite=self.dendrite,
        axons=[self.metagraph.axons[uid] for uid in miner_uids],
        synapse=synapse,
        send_block=self.block,
        timeout=QUERY_TIMEOUT_S,
    )

    bt.logging.info(f"Received {len(responses)} responses from {len(miner_uids)} miners")

    # Validate responses: dendrite.hotkey must match queried UID (readme §4.8 step 2)
    valid_responses = []
    valid_uids = []
    for response, uid in zip(responses, miner_uids):
        if response is None:
            continue
        expected_hotkey = self.metagraph.hotkeys[uid]
        if validate_response(response, expected_hotkey, self.metagraph):
            valid_responses.append(response)
            valid_uids.append(uid)

    bt.logging.info(f"Validated {len(valid_responses)} responses")

    if not valid_responses:
        bt.logging.warning("No valid responses received")
        time.sleep(5)
        return

    # ── Stages 3-5: Execution, Quality, Scoring ────────────────
    constraints = task.get("constraints", {})
    reference = task.get("reference", {})

    scores = []
    for response, uid in zip(valid_responses, valid_uids):
        # Stage 3: Sandboxed execution
        exec_result = execute_workflow(
            workflow_plan=response.workflow_plan,
            constraints=constraints,
            total_estimated_cost=response.total_estimated_cost or 0.01,
        )

        # Stage 4: Output quality evaluation (deterministic, no LLM judge)
        completion_ratio = (
            exec_result.steps_completed / exec_result.total_steps
            if exec_result.total_steps > 0
            else 0.0
        )

        output_quality = score_output_quality(
            task_type=task_type,
            output=exec_result.final_output,
            reference=reference,
        )

        # Stage 5: Composite scoring (four-dimension formula, readme §2.2)
        score_breakdown = compute_composite_score(
            output_quality=output_quality,
            completion_ratio=completion_ratio,
            actual_cost=exec_result.actual_cost,
            max_budget=constraints.get("max_budget_tao", 1.0),
            actual_latency=exec_result.actual_latency,
            max_latency=constraints.get("max_latency_seconds", 30.0),
            unplanned_retries=exec_result.unplanned_retries,
            timeouts=exec_result.timeouts,
            hard_failures=exec_result.hard_failures,
            budget_aborted=exec_result.budget_aborted,
        )

        composite_score = score_breakdown["S_composite"]
        scores.append(composite_score)

        bt.logging.debug(
            f"Miner {uid}: S={composite_score:.4f} "
            f"(success={score_breakdown['S_success']:.3f}, "
            f"cost={score_breakdown['S_cost']:.3f}, "
            f"latency={score_breakdown['S_latency']:.3f}, "
            f"reliability={score_breakdown['S_reliability']:.3f})"
        )

    # ── Stage 6: Rolling window update + lifecycle tracking ──────
    bt.logging.info(
        f"Scored {len(scores)} miners: mean={np.mean(scores):.4f}" if scores else "No scores"
    )

    # 6a. Update the score aggregator (equal-weight rolling 100-task window, readme §2.2)
    if hasattr(self, "score_aggregator"):
        for uid, score in zip(valid_uids, scores):
            self.score_aggregator.add_score(uid, score)
    else:
        bt.logging.warning("score_aggregator not initialised — cannot update rolling window")

    # 6b. Feed per-task scores into lifecycle tracker (readme §4.7)
    tracker.record_task_score(task_id, scores)

    # 6c. Increment N_min counter (readme §4.6)
    _tasks_executed_this_tempo += 1

    # 6d. At tempo boundary: flush lifecycle changes + log exec support eligibility
    current_tempo = self.block // TEMPO
    if current_tempo > _last_lifecycle_tempo:
        _last_lifecycle_tempo = current_tempo

        # Execution Support eligibility log (readme §4.6)
        eligible = _tasks_executed_this_tempo >= EXEC_SUPPORT_N_MIN
        bt.logging.info(
            f"TEMPO_BOUNDARY block={self.block}: "
            f"tasks_evaluated={_tasks_executed_this_tempo}/{EXEC_SUPPORT_N_MIN} — "
            f"EXEC_SUPPORT_ELIGIBLE: {eligible}"
        )
        _tasks_executed_this_tempo = 0

        # Flush lifecycle decisions and update benchmarks/v1.json
        tracker.on_tempo_end()

    time.sleep(2)  # Brief pause between evaluation rounds
