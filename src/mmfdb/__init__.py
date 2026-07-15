"""Small, stable public API for MMFDB.

Feature-specific APIs live in their respective submodules.  Keeping the root
package shallow avoids importing optional integrations and database machinery
for callers that only need runtime configuration.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .config import (
    DeploymentConfig,
    RuntimeConfig,
    apply_deployment_config,
    configure_runtime,
    get_runtime_config,
    load_client_config,
    load_deployment_config,
    reset_runtime_config,
)

if TYPE_CHECKING:
    from .repository import MFDatabase

__all__ = [
    "MFDatabase",
    "DeploymentConfig",
    "RuntimeConfig",
    "apply_deployment_config",
    "configure_runtime",
    "get_runtime_config",
    "load_client_config",
    "load_deployment_config",
    "reset_runtime_config",
]


def __getattr__(name: str) -> Any:
    """Load the database facade only when the public symbol is requested."""
    if name == "MFDatabase":
        from .repository import MFDatabase

        return MFDatabase
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
