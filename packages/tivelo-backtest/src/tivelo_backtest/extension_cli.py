"""Backtest CLI compatibility for optional signed extensions."""

from tivelo_contracts.errors import ErrorCode, TiveloError


def _manager():
    try:
        from tivelo_extensions import cli
    except ModuleNotFoundError as exc:
        if exc.name != "tivelo_extensions":
            raise
        raise TiveloError(
            ErrorCode.EXTENSION_INCOMPATIBLE,
            "Install tivelo-backtest[extensions] to use signed packages",
        ) from None
    return cli


def add_commands(parent):
    try:
        manager = _manager()
    except TiveloError:
        command = parent.add_parser(
            "extensions", help="Manage signed packages with tivelo-backtest[extensions]"
        )
        import argparse

        command.add_argument("arguments", nargs=argparse.REMAINDER)
        command.add_argument("--state")
    else:
        manager.add_commands(parent)


def manage(args):
    return _manager().manage(args)


def local_runtime(args):
    if args.extension_state is None:
        return None
    _manager()
    from .extension_host.price_views import LocalPriceViews

    # Each signed license supplies its own maximum admission time.
    return LocalPriceViews(args.extension_state, 2**31 - 1)
