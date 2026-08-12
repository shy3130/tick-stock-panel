from __future__ import annotations

import argparse

import pytest

from research.alphagpt.run_behavior_stability import _parse_seeds as parse_model_seeds
from research.alphagpt.run_rollout_expansion import _parse_seeds as parse_data_seeds


def test_multiseed_parsers_are_deterministic() -> None:
    assert parse_data_seeds("3, 1, 2") == (3, 1, 2)
    assert parse_model_seeds("11,12") == (11, 12)


@pytest.mark.parametrize(
    "parser,value",
    [
        (parse_data_seeds, ""),
        (parse_data_seeds, "1,1"),
        (parse_model_seeds, "1"),
        (parse_model_seeds, "2,2"),
    ],
)
def test_multiseed_parsers_reject_insufficient_or_duplicate_seeds(parser, value) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        parser(value)
