"""旧策略包服务的兼容入口; 实现统一收口到 resource_bundle。"""

from app.services.resource_bundle import (
    BUNDLE_CATEGORIES,
    BUNDLE_FORMAT,
    BUNDLE_VERSION,
    BundleSummary,
    ParsedBundle,
    StrategyBundleError,
    apply_bundle,
    build_bundle,
    collect_dependencies,
    collect_resources,
    inspect_bundle,
    parse_bundle,
)

__all__ = [
    "BUNDLE_CATEGORIES",
    "BUNDLE_FORMAT",
    "BUNDLE_VERSION",
    "BundleSummary",
    "ParsedBundle",
    "StrategyBundleError",
    "apply_bundle",
    "build_bundle",
    "collect_dependencies",
    "collect_resources",
    "inspect_bundle",
    "parse_bundle",
]
