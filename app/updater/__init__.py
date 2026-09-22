"""应用更新检测模块。"""

from .frida_script_puller import PullResult, pull_adapter_if_needed, should_disable_auto_adapter
from .version_check import (
    DEFAULT_ADAPTER_VERSION,
    DEFAULT_RELEASES_PAGE,
    VersionCheckResult,
    check_latest_version,
    get_local_app_version,
)

__all__ = [
    "DEFAULT_ADAPTER_VERSION",
    "DEFAULT_RELEASES_PAGE",
    "VersionCheckResult",
    "check_latest_version",
    "get_local_app_version",
    "PullResult",
    "pull_adapter_if_needed",
    "should_disable_auto_adapter",
]
