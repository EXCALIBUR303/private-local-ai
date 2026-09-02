"""
Privacy guards.

These are cheap, blunt, and deliberately paranoid. They exist so that a
misconfiguration cannot quietly turn a local-only app into a networked one.
"""
from __future__ import annotations

import ipaddress
from typing import Iterable

# Model names that route to a provider's servers rather than local weights.
# Ollama exposes hosted models with a `-cloud` suffix and namespaced remote
# models with a `/` in the name.
_CLOUD_MARKERS = ("-cloud", ":cloud", "cloud/")


class PrivacyViolation(RuntimeError):
    """Raised when a configuration would send data off this machine."""


def assert_loopback(host: str) -> None:
    """Reject any bind/connect target that is not this machine."""
    hostname = host.split("://")[-1].split("/")[0]
    if "]" in hostname:                      # bracketed IPv6, e.g. [::1]:11434
        hostname = hostname.split("]")[0].lstrip("[")
    elif hostname.count(":") == 1:
        hostname = hostname.split(":")[0]

    if hostname in ("localhost", ""):
        return
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError as exc:
        raise PrivacyViolation(
            f"Refusing to use non-loopback host {host!r}. "
            "This application only talks to 127.0.0.1."
        ) from exc
    if not ip.is_loopback:
        raise PrivacyViolation(
            f"Refusing to use non-loopback address {hostname!r}. "
            "This application only talks to 127.0.0.1."
        )


def assert_local_model(name: str, available: Iterable[str] | None = None) -> None:
    """
    Reject hosted/cloud model identifiers, and (when a model list is supplied)
    anything that is not actually present on this disk.
    """
    lowered = name.lower()
    for marker in _CLOUD_MARKERS:
        if marker in lowered:
            raise PrivacyViolation(
                f"Model {name!r} looks like a hosted/cloud model. "
                "This application only runs weights stored on this Mac."
            )
    if "/" in name:
        raise PrivacyViolation(
            f"Model {name!r} is namespaced to a remote registry account. "
            "Use a plain local model name such as 'qwen3:8b'."
        )
    if available is not None:
        names = set(available)
        if name not in names and f"{name}:latest" not in names:
            raise PrivacyViolation(
                f"Model {name!r} is not downloaded locally. "
                f"Run:  ollama pull {name}"
            )
