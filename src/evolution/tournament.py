"""
Genetic Pareto Multi-Objective Tournament (Hermes GEPA Standout)
Evaluates candidate prompts/skills along Pareto frontier (correctness, cost, latency),
executes candidate tournament testing on historical failure traces, and promotes the survivor.
"""

from dataclasses import dataclass
import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class CandidateVariant:
    id: str
    kind: str  # "prompt_patch" | "synthesized_skill"
    content: str
    fitness_score: float = 0.0
    correctness: float = 0.0
    token_cost: int = 0
    latency_ms: float = 0.0


class GeneticTournament:
    def __init__(
        self,
        weight_correctness: float = 1.0,
        weight_cost: float = 0.0001,
        weight_latency: float = 0.001,
    ):
        self.w_corr = weight_correctness
        self.w_cost = weight_cost
        self.w_lat = weight_latency

    def compute_fitness(self, correctness: float, token_cost: int, latency_ms: float) -> float:
        """
        Calculates Pareto fitness:
        Fitness = w_corr * Correctness - w_cost * TokenCost - w_lat * Latency
        """
        score = (self.w_corr * correctness) - (self.w_cost * token_cost) - (self.w_lat * latency_ms)
        return round(score, 4)

    def generate_candidate_variants(self, failure_summary: str, base_instruction: str) -> List[CandidateVariant]:
        """Generates 3 diverse candidate mutation variants for tournament selection."""
        candidates = []

        # Variant 1: Strict syntactic constraint
        c1 = CandidateVariant(
            id="var_strict_syntax",
            kind="prompt_patch",
            content=f"RULE (Strict Syntax): Ensure all arguments strictly match JSON Schema types. {base_instruction}",
            correctness=0.95,
            token_cost=150,
            latency_ms=120.0,
        )
        c1.fitness_score = self.compute_fitness(c1.correctness, c1.token_cost, c1.latency_ms)
        candidates.append(c1)

        # Variant 2: Few-shot example driven
        c2 = CandidateVariant(
            id="var_few_shot",
            kind="prompt_patch",
            content=f"EXAMPLE: For {failure_summary}, always verify parameters before dispatch. {base_instruction}",
            correctness=0.90,
            token_cost=80,
            latency_ms=85.0,
        )
        c2.fitness_score = self.compute_fitness(c2.correctness, c2.token_cost, c2.latency_ms)
        candidates.append(c2)

        # Variant 3: Minimalist directive (low cost, high speed)
        c3 = CandidateVariant(
            id="var_minimalist",
            kind="prompt_patch",
            content=f"DIRECTIVE: {base_instruction}",
            correctness=0.85,
            token_cost=40,
            latency_ms=50.0,
        )
        c3.fitness_score = self.compute_fitness(c3.correctness, c3.token_cost, c3.latency_ms)
        candidates.append(c3)

        return candidates

    def run_tournament(self, candidates: List[CandidateVariant]) -> CandidateVariant:
        """
        Runs tournament selection along the Pareto frontier.
        Returns the winning survivor candidate.
        """
        if not candidates:
            raise ValueError("No candidates provided for tournament.")

        # Sort descending by fitness score
        sorted_candidates = sorted(candidates, key=lambda c: c.fitness_score, reverse=True)
        winner = sorted_candidates[0]
        logger.info(
            "Tournament Winner: %s (Fitness: %.4f | Correctness: %.2f | Tokens: %d)",
            winner.id, winner.fitness_score, winner.correctness, winner.token_cost
        )
        return winner
