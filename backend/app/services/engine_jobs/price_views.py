"""Keep native code outside the TSP web process."""

import multiprocessing

from tivelo_backtest.extension_host.price_views import LocalPriceViews
from tivelo_contracts.errors import ErrorCode, TiveloError


class JobPriceViews(LocalPriceViews):
    def open(self, lock):
        if multiprocessing.parent_process() is None:
            raise TiveloError(
                ErrorCode.EXTENSION_INCOMPATIBLE, "工作台原生价格视图需要独立工作进程"
            )
        return super().open(lock)
