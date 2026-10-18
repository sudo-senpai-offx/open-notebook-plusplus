"""Residency control: deciding which models are loaded, from inside the guest.

The supervisor that actually starts and stops model servers lives in Termux,
outside proot, and is reached over loopback. See `client.py` for why the guest
cannot do this itself and why failure is always a hold.
"""

from open_notebook.residency.client import (
    ResidencyResult,
    RolePolicy,
    base_url,
    enabled,
    ensure_resident,
    policies,
    role_for_model,
    roles_for_models,
    state,
)

__all__ = [
    "ResidencyResult",
    "RolePolicy",
    "base_url",
    "enabled",
    "ensure_resident",
    "policies",
    "role_for_model",
    "roles_for_models",
    "state",
]
