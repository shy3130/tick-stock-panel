"""Resolve pinned native price providers for a dedicated local engine process."""

import os
from uuid import uuid4

from tivelo_contracts.errors import ErrorCode, TiveloError

from .installation import EngineInstallation, deployment_mode
from .store import EnginePackageStore


class LocalPriceViews:
    software_requirements = ("tivelo-backtest[extensions]",)

    def __init__(self, data_dir, max_seconds, *, required=None):
        self.packages = EnginePackageStore(data_dir)
        self.installation = EngineInstallation(data_dir)
        self.max_seconds = max_seconds
        self.required = required

    def resolve(self, provider_id):
        try:
            if self.required is None:
                return self.packages.resolve(provider_id)
            if self.required.capability_id != provider_id:
                raise ValueError
            self.packages.verified_pin(self.required)
            return self.required
        except ValueError as exc:
            raise TiveloError(
                ErrorCode.EXTENSION_INCOMPATIBLE, "计划锁定的引擎价格视图不可用"
            ) from exc

    def open(self, lock):
        from tivelo_backtest.native_entry import load_native_entry

        pin = next(p for p in lock.contents.extensions if p.kind == "price_view")
        if self.required is not None and pin != self.required:
            raise TiveloError(ErrorCode.DATA_CONFLICT, "任务价格视图与提交时的版本不一致")
        try:
            path, release = self.packages.loadable_entry(pin)
        except (ValueError, OSError) as exc:
            raise TiveloError(
                ErrorCode.EXTENSION_INCOMPATIBLE, "任务锁定的引擎制品未通过验证"
            ) from exc
        arguments = dict(
            lock=lock,
            attempt_id=uuid4(),
            max_run_seconds=self.max_seconds,
            deployment=deployment_mode(),
        )
        if release.license_kind == "commercial":
            try:
                envelope = self.installation.lease(release.id)
                public, prove = self.installation.device("run", bytes.fromhex(lock.lock_id))
            except (ValueError, OSError) as exc:
                raise TiveloError(
                    ErrorCode.LICENSE_REQUIRED, "此专业扩展需要本机有效使用凭证"
                ) from exc
            arguments.update(lease=envelope, device_public_key=public, prove_device=prove)
        os.environ["TIVELO_LICENSE_STATE_DIR"] = str(
            self.installation.root / "research/engine/installation/observations"
        )
        return load_native_entry(path).create_provider(**arguments)
