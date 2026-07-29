"""逐 token rollout 采集与 P10 evolution 教师轨迹重放。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any, Sequence

from research.alphagpt.environment import STOP_ACTION, AlphaEnv, AlphaEnvConfig
from research.alphagpt.policy import (
    PolicyObservation,
    ReplayTokenPolicy,
    TokenPolicy,
    validate_policy_action,
)
from research.alphagpt.pool import formula_hash


@dataclass(frozen=True)
class RolloutStep:
    step_index: int
    observation: PolicyObservation
    action: str
    action_id: int
    done: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_index": self.step_index,
            "observation": self.observation.to_dict(),
            "action": self.action,
            "action_id": self.action_id,
            "done": self.done,
        }


@dataclass(frozen=True)
class RolloutEpisode:
    episode_id: str
    policy_name: str
    seed: int
    steps: tuple[RolloutStep, ...]
    formula_tokens: tuple[str, ...]
    formula_hash: str
    evaluation_status: str = "pending"
    final_reward: float | None = None
    reward_breakdown: dict[str, Any] = field(default_factory=dict)
    training_fold_metrics: tuple[dict[str, Any], ...] = ()
    provenance: dict[str, Any] = field(default_factory=dict)

    def to_manifest_entry(self, *, split: str) -> dict[str, Any]:
        return {
            "episode_id": self.episode_id,
            "policy_name": self.policy_name,
            "seed": self.seed,
            "split": split,
            "n_steps": len(self.steps),
            "formula": " ".join(self.formula_tokens),
            "formula_tokens": list(self.formula_tokens),
            "formula_hash": self.formula_hash,
            "evaluation_status": self.evaluation_status,
            "final_reward": self.final_reward,
            "reward_breakdown": self.reward_breakdown,
            "training_fold_metrics": list(self.training_fold_metrics),
            "provenance": self.provenance,
        }


@dataclass(frozen=True)
class RolloutCollection:
    episodes: tuple[RolloutEpisode, ...]
    failures: tuple[dict[str, Any], ...]
    source_seed: int
    data_fingerprint: str
    environment_config: dict[str, Any]
    source_metadata: dict[str, Any] = field(default_factory=dict)


def run_policy_episode(
    *,
    policy: TokenPolicy,
    environment_config: AlphaEnvConfig,
    episode_id: str,
    seed: int,
) -> RolloutEpisode:
    """运行一个完整 episode，并在每一步中央校验 policy action。"""

    environment = AlphaEnv(environment_config)
    environment.reset(seed=seed)
    policy.reset(seed=seed)
    steps: list[RolloutStep] = []
    while not environment.terminated:
        observation = PolicyObservation.from_environment(environment)
        action = policy.select_action(observation)
        action_id = validate_policy_action(observation, action)
        _, done = environment.step(action)
        steps.append(
            RolloutStep(
                step_index=len(steps),
                observation=observation,
                action=action,
                action_id=action_id,
                done=done,
            )
        )
    if not steps or steps[-1].action != STOP_ACTION:
        raise RuntimeError("policy episode did not end with STOP")
    if not environment.validate_formula(environment.tokens):
        raise RuntimeError("policy episode produced a StackVM-invalid formula")
    tokens = tuple(environment.tokens)
    return RolloutEpisode(
        episode_id=episode_id,
        policy_name=policy.name,
        seed=seed,
        steps=tuple(steps),
        formula_tokens=tokens,
        formula_hash=formula_hash(tokens),
    )


def replay_teacher_formula(
    *,
    tokens: Sequence[str],
    environment_config: AlphaEnvConfig,
    episode_id: str,
    seed: int,
    policy_name: str = "evolution_teacher_replay",
) -> RolloutEpisode:
    policy = ReplayTokenPolicy([*tokens, STOP_ACTION], name=policy_name)
    return run_policy_episode(
        policy=policy,
        environment_config=environment_config,
        episode_id=episode_id,
        seed=seed,
    )


def collect_p10_evolution_rollouts(
    payload: dict[str, Any],
    *,
    max_episodes: int | None = None,
    minimum_reward: float | None = None,
) -> RolloutCollection:
    """只从 P10 evolution 候选池的训练记录构造离线教师数据。"""

    if payload.get("phase") != "P10 AlphaGPT closed loop v1":
        raise ValueError("source artifact is not P10 AlphaGPT v1")
    config = payload["config"]
    source_seed = int(config["seed"])
    environment_data = dict(config["environment"])
    environment_config = AlphaEnvConfig(**environment_data)
    data_fingerprint = str(config["search"]["data_fingerprint"])
    pool = payload["searches"]["evolution"]["pool"]
    candidates = [
        candidate
        for candidate in pool["candidates"]
        if candidate.get("status") == "accepted"
    ]
    candidates.sort(
        key=lambda candidate: (
            -float(candidate["reward"]["total"]),
            str(candidate["formula_hash"]),
        )
    )
    if minimum_reward is not None:
        candidates = [
            candidate
            for candidate in candidates
            if float(candidate["reward"]["total"]) >= minimum_reward
        ]
    if max_episodes is not None:
        if max_episodes <= 0:
            raise ValueError("max_episodes must be > 0")
        candidates = candidates[:max_episodes]

    episodes: list[RolloutEpisode] = []
    failures: list[dict[str, Any]] = []
    for index, candidate in enumerate(candidates):
        candidate_id = str(candidate["candidate_id"])
        try:
            episode = replay_teacher_formula(
                tokens=candidate["tokens"],
                environment_config=environment_config,
                episode_id=f"p11a_{candidate_id}",
                seed=source_seed + index,
            )
            if episode.formula_hash != candidate["formula_hash"]:
                raise ValueError("replayed formula hash does not match P10 candidate")
            episodes.append(
                replace(
                    episode,
                    evaluation_status="training_evaluated",
                    final_reward=float(candidate["reward"]["total"]),
                    reward_breakdown=dict(candidate["reward"]),
                    training_fold_metrics=tuple(candidate["fold_metrics"]),
                    provenance={
                        "source_phase": "P10",
                        "source_search_method": "evolution",
                        "source_candidate_id": candidate_id,
                        "generation_method": candidate["generation_method"],
                        "parent_formulas": list(candidate["parent_formulas"]),
                        "selection": "accepted evolution pool candidate",
                    },
                )
            )
        except Exception as exc:
            failures.append(
                {
                    "reason": "teacher_replay_failure",
                    "candidate_id": candidate_id,
                    "formula": candidate.get("formula"),
                    "formula_hash": candidate.get("formula_hash"),
                    "exception_type": type(exc).__name__,
                    "message": str(exc),
                }
            )
    return RolloutCollection(
        episodes=tuple(episodes),
        failures=tuple(failures),
        source_seed=source_seed,
        data_fingerprint=data_fingerprint,
        environment_config=asdict(environment_config),
    )
