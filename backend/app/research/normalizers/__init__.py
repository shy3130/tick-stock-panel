"""Normalize five result profiles without recomputing evaluator statistics."""

from app.research.adapters import _norm


def normalize(raw, profile):
    return _norm(profile, raw)


__all__ = ["normalize"]
