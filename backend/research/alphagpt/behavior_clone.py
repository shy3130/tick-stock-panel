"""零新增依赖的 masked behavior cloning 基线。

包含一个 n-gram 对照和一个可训练的单层、单头 NumPy Transformer encoder。
模型只预测下一个 token；最终动作仍必须经过 ``MaskedLogitPolicy``。
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from research.alphagpt.dataset import read_transitions
from research.alphagpt.policy import PolicyObservation


@dataclass(frozen=True)
class BehaviorExample:
    token_ids: tuple[int, ...]
    action_mask: tuple[bool, ...]
    target_id: int
    split: str
    episode_id: str
    final_reward: float = 0.0
    sample_weight: float = 1.0


def load_behavior_examples(path: Path) -> list[BehaviorExample]:
    examples: list[BehaviorExample] = []
    for item in read_transitions(path):
        examples.append(
            BehaviorExample(
                token_ids=tuple(int(value) for value in item["observation"]["token_ids"]),
                action_mask=tuple(bool(value) for value in item["observation"]["action_mask"]),
                target_id=int(item["action_id"]),
                split=str(item["split"]),
                episode_id=str(item["episode_id"]),
                final_reward=float(item["outcome"]["final_training_reward"]),
            )
        )
    if not examples:
        raise ValueError("behavior dataset is empty")
    return examples


class NGramBehaviorPolicy:
    """带逐级 backoff 的 token n-gram 基线。"""

    def __init__(self, *, action_size: int, order: int = 2, smoothing: float = 0.25) -> None:
        if action_size <= 0 or order < 0 or smoothing <= 0:
            raise ValueError("invalid n-gram configuration")
        self.action_size = action_size
        self.order = order
        self.smoothing = smoothing
        self._counts: dict[tuple[int, ...], np.ndarray] = {}

    def fit(self, examples: Sequence[BehaviorExample]) -> None:
        self._counts = {}
        for example in examples:
            for length in range(self.order + 1):
                context = tuple(example.token_ids[-length:]) if length else ()
                counts = self._counts.setdefault(
                    context,
                    np.zeros(self.action_size, dtype=float),
                )
                counts[example.target_id] += example.sample_weight

    def logits_for_tokens(self, token_ids: Sequence[int]) -> np.ndarray:
        for length in range(min(self.order, len(token_ids)), -1, -1):
            context = tuple(token_ids[-length:]) if length else ()
            if context in self._counts:
                return np.log(self._counts[context] + self.smoothing)
        return np.zeros(self.action_size, dtype=float)

    def logits(self, observation: PolicyObservation) -> np.ndarray:
        return self.logits_for_tokens(observation.token_ids)

    def evaluate(self, examples: Sequence[BehaviorExample]) -> dict[str, float]:
        losses: list[float] = []
        correct = 0
        for example in examples:
            logits = self.logits_for_tokens(example.token_ids)
            probabilities = _masked_probabilities(logits, example.action_mask)
            losses.append(-math.log(max(1e-12, float(probabilities[example.target_id]))))
            if int(np.argmax(probabilities)) == example.target_id:
                correct += 1
        return {
            "nll": float(np.mean(losses)) if losses else float("nan"),
            "accuracy": correct / len(examples) if examples else float("nan"),
        }


@dataclass(frozen=True)
class TinyTransformerConfig:
    action_size: int
    max_prefix_length: int = 10
    d_model: int = 24
    seed: int = 20260724
    learning_rate: float = 0.01
    weight_decay: float = 1e-4
    gradient_clip: float = 5.0

    def __post_init__(self) -> None:
        if self.action_size <= 0 or self.max_prefix_length <= 0 or self.d_model <= 0:
            raise ValueError("model dimensions must be positive")
        if self.learning_rate <= 0 or self.gradient_clip <= 0:
            raise ValueError("optimizer values must be positive")


class NumpyMaskedTransformer:
    """单层、单头 self-attention encoder；CLS 位置预测下一动作。"""

    def __init__(self, config: TinyTransformerConfig) -> None:
        self.config = config
        self.cls_id = config.action_size
        self.pad_id = config.action_size + 1
        self.input_vocab_size = config.action_size + 2
        self.sequence_length = config.max_prefix_length + 1
        rng = np.random.default_rng(config.seed)
        d = config.d_model
        scale = 1.0 / math.sqrt(d)
        self.params: dict[str, np.ndarray] = {
            "embedding": rng.normal(0.0, scale, (self.input_vocab_size, d)),
            "position": rng.normal(0.0, scale, (self.sequence_length, d)),
            "wq": rng.normal(0.0, scale, (d, d)),
            "wk": rng.normal(0.0, scale, (d, d)),
            "wv": rng.normal(0.0, scale, (d, d)),
            "wo": rng.normal(0.0, scale, (d, d)),
            "classifier": rng.normal(0.0, scale, (d, config.action_size)),
            "bias": np.zeros(config.action_size, dtype=float),
        }
        self._adam_m = {name: np.zeros_like(value) for name, value in self.params.items()}
        self._adam_v = {name: np.zeros_like(value) for name, value in self.params.items()}
        self._adam_step = 0

    def _arrays(
        self,
        examples: Sequence[BehaviorExample],
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        batch = len(examples)
        ids = np.full((batch, self.sequence_length), self.pad_id, dtype=np.int64)
        valid = np.zeros((batch, self.sequence_length), dtype=bool)
        action_masks = np.zeros((batch, self.config.action_size), dtype=bool)
        targets = np.zeros(batch, dtype=np.int64)
        ids[:, 0] = self.cls_id
        valid[:, 0] = True
        for row, example in enumerate(examples):
            if len(example.token_ids) > self.config.max_prefix_length:
                raise ValueError("prefix exceeds configured maximum")
            length = len(example.token_ids)
            if length:
                ids[row, 1 : length + 1] = example.token_ids
                valid[row, 1 : length + 1] = True
            action_masks[row] = example.action_mask
            targets[row] = example.target_id
            if not action_masks[row, targets[row]]:
                raise ValueError("training target is masked")
        return ids, valid, action_masks, targets

    def _forward(
        self,
        ids: np.ndarray,
        valid: np.ndarray,
        action_masks: np.ndarray,
    ) -> tuple[np.ndarray, dict[str, np.ndarray]]:
        p = self.params
        x = p["embedding"][ids] + p["position"][None, :, :]
        q = x @ p["wq"]
        k = x @ p["wk"]
        v = x @ p["wv"]
        scores = q @ np.swapaxes(k, 1, 2) / math.sqrt(self.config.d_model)
        scores = np.where(valid[:, None, :], scores, -1e9)
        scores -= np.max(scores, axis=-1, keepdims=True)
        attention = np.exp(scores)
        attention /= np.sum(attention, axis=-1, keepdims=True)
        attended = attention @ v
        z = attended @ p["wo"] + x
        hidden = np.tanh(z)
        logits = hidden[:, 0, :] @ p["classifier"] + p["bias"]
        masked = np.where(action_masks, logits, -1e9)
        cache = {
            "ids": ids,
            "valid": valid,
            "action_masks": action_masks,
            "x": x,
            "q": q,
            "k": k,
            "v": v,
            "attention": attention,
            "attended": attended,
            "z": z,
            "hidden": hidden,
            "logits": logits,
        }
        return masked, cache

    @staticmethod
    def _loss_probabilities(
        masked_logits: np.ndarray,
        targets: np.ndarray,
        sample_weights: np.ndarray | None = None,
    ) -> tuple[float, np.ndarray]:
        shifted = masked_logits - np.max(masked_logits, axis=1, keepdims=True)
        probabilities = np.exp(shifted)
        probabilities /= probabilities.sum(axis=1, keepdims=True)
        selected = probabilities[np.arange(len(targets)), targets]
        losses = -np.log(np.maximum(selected, 1e-12))
        if sample_weights is None:
            loss = float(np.mean(losses))
        else:
            loss = float(np.sum(losses * sample_weights) / np.sum(sample_weights))
        return loss, probabilities

    def _gradients(
        self,
        cache: dict[str, np.ndarray],
        probabilities: np.ndarray,
        targets: np.ndarray,
        sample_weights: np.ndarray,
    ) -> dict[str, np.ndarray]:
        p = self.params
        batch = len(targets)
        dlogits = probabilities.copy()
        dlogits[np.arange(batch), targets] -= 1.0
        dlogits *= sample_weights[:, None] / np.sum(sample_weights)
        dlogits = np.where(cache["action_masks"], dlogits, 0.0)

        hidden = cache["hidden"]
        gradients: dict[str, np.ndarray] = {}
        gradients["classifier"] = hidden[:, 0, :].T @ dlogits
        gradients["bias"] = dlogits.sum(axis=0)
        dhidden = np.zeros_like(hidden)
        dhidden[:, 0, :] = dlogits @ p["classifier"].T
        dz = dhidden * (1.0 - hidden**2)

        attended = cache["attended"]
        flat_attended = attended.reshape(-1, self.config.d_model)
        flat_dz = dz.reshape(-1, self.config.d_model)
        gradients["wo"] = flat_attended.T @ flat_dz
        dattended = dz @ p["wo"].T
        dx = dz.copy()

        attention = cache["attention"]
        value = cache["v"]
        dattn = dattended @ np.swapaxes(value, 1, 2)
        dvalue = np.swapaxes(attention, 1, 2) @ dattended
        dscores = attention * (
            dattn - np.sum(dattn * attention, axis=-1, keepdims=True)
        )
        dscores = np.where(cache["valid"][:, None, :], dscores, 0.0)
        scale = 1.0 / math.sqrt(self.config.d_model)
        dquery = dscores @ cache["k"] * scale
        dkey = np.swapaxes(dscores, 1, 2) @ cache["q"] * scale

        x = cache["x"]
        flat_x = x.reshape(-1, self.config.d_model)
        gradients["wq"] = flat_x.T @ dquery.reshape(-1, self.config.d_model)
        gradients["wk"] = flat_x.T @ dkey.reshape(-1, self.config.d_model)
        gradients["wv"] = flat_x.T @ dvalue.reshape(-1, self.config.d_model)
        dx += dquery @ p["wq"].T
        dx += dkey @ p["wk"].T
        dx += dvalue @ p["wv"].T
        dx = np.where(cache["valid"][:, :, None], dx, 0.0)

        gradients["embedding"] = np.zeros_like(p["embedding"])
        np.add.at(gradients["embedding"], cache["ids"], dx)
        gradients["position"] = dx.sum(axis=0)
        return gradients

    def _update(self, gradients: dict[str, np.ndarray]) -> None:
        squared_norm = sum(float(np.sum(gradient**2)) for gradient in gradients.values())
        norm = math.sqrt(squared_norm)
        clip_scale = min(1.0, self.config.gradient_clip / (norm + 1e-12))
        self._adam_step += 1
        beta1, beta2 = 0.9, 0.999
        for name, parameter in self.params.items():
            gradient = gradients[name] * clip_scale
            if name not in {"bias"}:
                gradient = gradient + self.config.weight_decay * parameter
            self._adam_m[name] = beta1 * self._adam_m[name] + (1.0 - beta1) * gradient
            self._adam_v[name] = beta2 * self._adam_v[name] + (1.0 - beta2) * gradient**2
            m_hat = self._adam_m[name] / (1.0 - beta1**self._adam_step)
            v_hat = self._adam_v[name] / (1.0 - beta2**self._adam_step)
            parameter -= self.config.learning_rate * m_hat / (np.sqrt(v_hat) + 1e-8)

    def train(
        self,
        train_examples: Sequence[BehaviorExample],
        validation_examples: Sequence[BehaviorExample],
        *,
        epochs: int = 200,
        batch_size: int = 32,
        early_stopping_patience: int = 40,
    ) -> dict[str, Any]:
        if not train_examples or not validation_examples:
            raise ValueError("both train and validation examples are required")
        if epochs <= 0 or batch_size <= 0 or early_stopping_patience <= 0:
            raise ValueError("epochs, batch_size and patience must be positive")
        rng = np.random.default_rng(self.config.seed)
        initial = {
            "train": self.evaluate(train_examples),
            "validation": self.evaluate(validation_examples),
        }
        history: list[dict[str, float | int]] = []
        best_epoch = 0
        best_validation = initial["validation"]
        best_params = {name: value.copy() for name, value in self.params.items()}
        stale_epochs = 0
        epochs_ran = 0
        indices = np.arange(len(train_examples))
        for epoch in range(1, epochs + 1):
            epochs_ran = epoch
            rng.shuffle(indices)
            losses: list[float] = []
            for start in range(0, len(indices), batch_size):
                batch = [train_examples[index] for index in indices[start : start + batch_size]]
                ids, valid, masks, targets = self._arrays(batch)
                masked, cache = self._forward(ids, valid, masks)
                sample_weights = np.asarray(
                    [example.sample_weight for example in batch],
                    dtype=float,
                )
                if not np.all(np.isfinite(sample_weights)) or np.any(sample_weights <= 0):
                    raise ValueError("sample weights must be positive and finite")
                loss, probabilities = self._loss_probabilities(
                    masked,
                    targets,
                    sample_weights,
                )
                gradients = self._gradients(
                    cache,
                    probabilities,
                    targets,
                    sample_weights,
                )
                self._update(gradients)
                losses.append(loss)
            validation = self.evaluate(validation_examples)
            if validation["nll"] < best_validation["nll"] - 1e-6:
                best_epoch = epoch
                best_validation = validation
                best_params = {name: value.copy() for name, value in self.params.items()}
                stale_epochs = 0
            else:
                stale_epochs += 1
            if epoch == 1 or epoch == epochs or epoch % max(1, epochs // 10) == 0:
                history.append(
                    {
                        "epoch": epoch,
                        "train_batch_nll": float(np.mean(losses)),
                        "validation_nll": validation["nll"],
                        "validation_accuracy": validation["accuracy"],
                    }
                )
            if stale_epochs >= early_stopping_patience:
                break
        for name, value in best_params.items():
            self.params[name][...] = value
        return {
            "initial": initial,
            "final": {
                "train": self.evaluate(train_examples),
                "validation": self.evaluate(validation_examples),
            },
            "early_stopping": {
                "selected_epoch": best_epoch,
                "epochs_ran": epochs_ran,
                "patience": early_stopping_patience,
                "stopped_early": epochs_ran < epochs,
                "selection_metric": "rollout validation NLL",
                "best_validation": best_validation,
            },
            "history": history,
        }

    def evaluate(self, examples: Sequence[BehaviorExample]) -> dict[str, float]:
        ids, valid, masks, targets = self._arrays(examples)
        masked, _ = self._forward(ids, valid, masks)
        loss, probabilities = self._loss_probabilities(masked, targets)
        predictions = np.argmax(probabilities, axis=1)
        return {
            "nll": loss,
            "accuracy": float(np.mean(predictions == targets)),
        }

    def logits_for_tokens(
        self,
        token_ids: Sequence[int],
        action_mask: Sequence[bool],
    ) -> np.ndarray:
        example = BehaviorExample(
            token_ids=tuple(token_ids),
            action_mask=tuple(action_mask),
            target_id=int(np.flatnonzero(action_mask)[0]),
            split="inference",
            episode_id="inference",
        )
        ids, valid, masks, _ = self._arrays([example])
        _, cache = self._forward(ids, valid, masks)
        return cache["logits"][0].copy()

    def logits(self, observation: PolicyObservation) -> np.ndarray:
        return self.logits_for_tokens(observation.token_ids, observation.action_mask)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        metadata = json.dumps(asdict(self.config), sort_keys=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("wb") as handle:
            np.savez_compressed(handle, metadata=np.array(metadata), **self.params)
        temporary.replace(path)

    @classmethod
    def load(cls, path: Path) -> NumpyMaskedTransformer:
        with np.load(path, allow_pickle=False) as archive:
            config = TinyTransformerConfig(**json.loads(str(archive["metadata"])))
            model = cls(config)
            for name in model.params:
                model.params[name][...] = archive[name]
        return model


def _masked_probabilities(
    logits: Sequence[float] | np.ndarray,
    action_mask: Sequence[bool],
) -> np.ndarray:
    values = np.asarray(logits, dtype=float)
    mask = np.asarray(action_mask, dtype=bool)
    masked = np.where(mask, values, -1e9)
    shifted = masked - np.max(masked)
    probabilities = np.exp(shifted)
    probabilities /= probabilities.sum()
    return probabilities


def prepare_reward_conditioned_examples(
    examples: Sequence[BehaviorExample],
    *,
    mode: str,
    elite_quantile: float = 0.60,
    reward_temperature: float = 2.0,
    minimum_weight: float = 0.10,
    maximum_weight: float = 10.0,
) -> tuple[list[BehaviorExample], dict[str, Any]]:
    """只用训练 episode 奖励构建 uniform / reward-weighted / elite 样本。"""

    if not examples:
        raise ValueError("training examples are required")
    if any(example.split != "train" for example in examples):
        raise ValueError("reward conditioning accepts training examples only")
    if mode not in {"uniform", "reward_weighted", "elite"}:
        raise ValueError(f"unsupported reward conditioning mode: {mode}")
    if not 0.0 < elite_quantile < 1.0:
        raise ValueError("elite_quantile must be within (0, 1)")
    if reward_temperature <= 0 or minimum_weight <= 0 or maximum_weight < minimum_weight:
        raise ValueError("invalid reward weighting configuration")

    episode_rewards: dict[str, float] = {}
    for example in examples:
        previous = episode_rewards.setdefault(example.episode_id, example.final_reward)
        if not math.isclose(previous, example.final_reward, rel_tol=0.0, abs_tol=1e-12):
            raise ValueError("an episode contains inconsistent final rewards")
    rewards = np.asarray(list(episode_rewards.values()), dtype=float)
    if not np.all(np.isfinite(rewards)):
        raise ValueError("episode rewards must be finite")

    threshold: float | None = None
    episode_weights: dict[str, float]
    if mode == "uniform":
        episode_weights = {episode_id: 1.0 for episode_id in episode_rewards}
    elif mode == "elite":
        threshold = float(np.quantile(rewards, elite_quantile))
        episode_weights = {
            episode_id: 1.0
            for episode_id, reward in episode_rewards.items()
            if reward >= threshold
        }
    else:
        median = float(np.median(rewards))
        raw = {
            episode_id: float(
                np.clip(
                    math.exp(
                        float(
                            np.clip(
                                (reward - median) / reward_temperature,
                                -20.0,
                                20.0,
                            )
                        )
                    ),
                    minimum_weight,
                    maximum_weight,
                )
            )
            for episode_id, reward in episode_rewards.items()
        }
        mean_weight = float(np.mean(list(raw.values())))
        episode_weights = {
            episode_id: weight / mean_weight for episode_id, weight in raw.items()
        }

    conditioned = [
        replace(example, sample_weight=episode_weights[example.episode_id])
        for example in examples
        if example.episode_id in episode_weights
    ]
    selected_rewards = np.asarray(
        [episode_rewards[episode_id] for episode_id in episode_weights],
        dtype=float,
    )
    weights = np.asarray(list(episode_weights.values()), dtype=float)
    audit = {
        "mode": mode,
        "elite_quantile": elite_quantile if mode == "elite" else None,
        "elite_reward_threshold": threshold,
        "reward_temperature": reward_temperature if mode == "reward_weighted" else None,
        "input_episodes": len(episode_rewards),
        "selected_episodes": len(episode_weights),
        "input_transitions": len(examples),
        "selected_transitions": len(conditioned),
        "selected_reward": {
            "min": float(np.min(selected_rewards)),
            "median": float(np.median(selected_rewards)),
            "mean": float(np.mean(selected_rewards)),
            "max": float(np.max(selected_rewards)),
        },
        "sample_weight": {
            "min": float(np.min(weights)),
            "mean": float(np.mean(weights)),
            "max": float(np.max(weights)),
        },
        "boundary": "training episode rewards only",
    }
    return conditioned, audit
