"""从 P10 evolution 训练候选构建 P11-A 离线 token rollout 数据集。"""

from __future__ import annotations

import argparse
import hashlib
import json

from research.alphagpt.dataset import write_rollout_dataset
from research.alphagpt.rollouts import collect_p10_evolution_rollouts
from research.paths import FACTOR_ARTIFACTS_DIR, ensure_artifact_dirs

SOURCE = FACTOR_ARTIFACTS_DIR / "alphagpt_v1.json"
DATASET_OUT = FACTOR_ARTIFACTS_DIR / "alphagpt_rollouts_v1.jsonl"
MANIFEST_OUT = FACTOR_ARTIFACTS_DIR / "alphagpt_rollouts_v1_manifest.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max-episodes",
        type=int,
        default=None,
        help="按训练奖励降序最多保留多少个 accepted evolution 候选",
    )
    parser.add_argument("--minimum-reward", type=float, default=None)
    parser.add_argument("--validation-fraction", type=float, default=0.20)
    return parser


def run(args: argparse.Namespace) -> dict[str, object]:
    ensure_artifact_dirs()
    source_bytes = SOURCE.read_bytes()
    payload = json.loads(source_bytes.decode("utf-8"))
    collection = collect_p10_evolution_rollouts(
        payload,
        max_episodes=args.max_episodes,
        minimum_reward=args.minimum_reward,
    )
    return write_rollout_dataset(
        collection,
        dataset_path=DATASET_OUT,
        manifest_path=MANIFEST_OUT,
        source_artifact_sha256=hashlib.sha256(source_bytes).hexdigest(),
        validation_fraction=args.validation_fraction,
    )


def main() -> None:
    args = build_parser().parse_args()
    manifest = run(args)
    counts = manifest["counts"]
    print(
        "[alphagpt-rollouts] complete | "
        f"episodes={counts['episodes']} transitions={counts['transitions']} "
        f"failures={counts['failures']} | {DATASET_OUT}",
        flush=True,
    )


if __name__ == "__main__":
    main()
