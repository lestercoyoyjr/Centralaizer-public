"""
Bayesian trust scoring — gap #2 from SuperLocalMemory (arXiv:2603.02240).

Every memory write is assigned a trust score in [0, 1].
Writes below settings.trust_threshold go to quarantine instead of main store.

Score factors
-------------
1. Agent prior      — agents accumulate a reputation from past write quality (default 0.8)
2. Hedged language  — speculative / uncertain content is penalised, graded by how much of
                      the write hedges; enough hedging pushes even a trusted agent into
                      quarantine for human review
3. Contradiction    — does this content conflict with existing high-trust memories?
(Planned: corroboration boost when a recent independent write makes the same claim.)
"""
from __future__ import annotations
import re
from config.settings import settings
from core.storage import vector_store

_HEDGE_PATTERNS = re.compile(
    r"\b(maybe|perhaps|i think|i believe|possibly|probably|might be|may be|could be|"
    r"not sure|unsure|uncertain|i guess|seems like|apparently|presumably|i assume)\b",
    re.I,
)

# Hedging is graded: deduction = _HEDGE_PENALTY * (fraction of hedged sentences).
# With the default agent prior (0.8) and threshold (0.6), a write that hedges in
# ~40%+ of its sentences falls into quarantine — so speculative claims actually
# get caught instead of squeaking past at exactly the threshold.
_HEDGE_PENALTY = 0.5
_SPECULATION_THRESHOLD = 0.34  # at/above this hedge ratio, name it in the reason string


def _hedge_ratio(text: str) -> float:
    sentences = [s.strip() for s in text.split(".") if s.strip()]
    if not sentences:
        return 0.0
    hedged = sum(1 for s in sentences if _HEDGE_PATTERNS.search(s))
    return hedged / len(sentences)


def _contradiction_penalty(content: str) -> float:
    """
    Search for existing memories similar to this content.
    If we find high-trust memories that directly contradict it, apply a penalty.
    Crude heuristic: similarity > 0.85 but content starts with negation of existing.
    """
    similar = vector_store.search(content, n_results=5)
    penalty = 0.0
    negation = re.compile(r"^\s*(not|no|never|don't|doesn't|isn't|aren't|wasn't)\b", re.I)
    for hit in similar:
        if hit["distance"] < 0.15:  # very similar (cosine distance, lower = closer)
            if negation.match(hit["content"]) != negation.match(content):
                penalty = max(penalty, 0.25)
    return penalty


def compute_trust(
    agent_id: str,
    content: str,
    agent_history: dict[str, float] | None = None,
) -> tuple[float, str]:
    """
    Returns (trust_score, reason_string).
    reason_string explains why the score is what it is — stored in quarantine entries.
    """
    agent_history = agent_history or {}
    reasons: list[str] = []

    # 1. Agent prior — default 0.8 for new agents
    agent_prior = agent_history.get(agent_id, 0.8)
    score = agent_prior

    # 2. Hedged language — graded penalty, applied whenever the write hedges at all.
    hr = _hedge_ratio(content)
    if hr > 0:
        deduction = round(_HEDGE_PENALTY * hr, 4)
        score -= deduction
        if hr >= _SPECULATION_THRESHOLD:
            reasons.append(f"speculative/hedged language ({hr:.0%} of sentences)")

    # 3. Contradiction with existing memories
    penalty = _contradiction_penalty(content)
    if penalty:
        score -= penalty
        reasons.append(f"potential contradiction with existing memory (-{penalty:.2f})")

    score = max(0.0, min(1.0, score))
    reason = "; ".join(reasons) if reasons else "ok"
    return score, reason


def update_agent_prior(
    agent_history: dict[str, float],
    agent_id: str,
    approved: bool,
) -> dict[str, float]:
    """
    Simple Bayesian update: approved write nudges prior up, rejected nudges it down.
    """
    prior = agent_history.get(agent_id, 0.8)
    if approved:
        new_prior = prior + 0.05 * (1.0 - prior)   # approaches 1.0 asymptotically
    else:
        new_prior = prior - 0.05 * prior            # approaches 0.0 asymptotically
    agent_history[agent_id] = round(new_prior, 4)
    return agent_history
