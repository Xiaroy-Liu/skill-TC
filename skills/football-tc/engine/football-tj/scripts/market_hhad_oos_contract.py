#!/usr/bin/env python3
"""Stable identity for the HHAD OOS authorization contract."""
from __future__ import annotations

import hashlib
import json
from typing import Any


def contract_sha256(registry: dict[str, Any]) -> str:
    gate = ((registry.get("gates") or {}).get("market_hhad_oos") or {})
    stable_gate = {key: value for key, value in gate.items() if key not in {"registered_artifact", "current_evidence"}}
    contract = {"rule_version": registry.get("rule_version"), "gate": stable_gate}
    encoded = json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
