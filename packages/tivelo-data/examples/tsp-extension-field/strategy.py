"""Example trusted strategy for a pinned positive integer rank (one is best)."""

import json

from tivelo_contracts.execution import Signal, SignalBatch


class RankStrategy:
    def __init__(self, field_sha256):
        self.field_sha256 = field_sha256

    def decide(self, context):
        eligible = {row.instrument_id for row in context.openings if row.state == "tradable"}
        ranks = [
            item
            for item in context.extension_values
            if item.field_sha256 == self.field_sha256
            and item.instrument_id in eligible
            and item.int_value is not None
            and item.int_value > 0
        ]
        signals = ()
        if ranks and not context.portfolio.positions:
            first = min(ranks, key=lambda item: (item.int_value, item.instrument_id))
            signals = (
                Signal(
                    instrument_id=first.instrument_id,
                    decision_at=context.now,
                    side="buy",
                    score=-first.int_value,
                    source_id="example.rank",
                    target_weight="0.5",
                ),
            )
        return SignalBatch(decision_at=context.now, signals=signals)


def build(*, parameters_json, dependencies):
    return RankStrategy(json.loads(parameters_json)["field_sha256"])
