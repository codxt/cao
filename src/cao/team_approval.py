"""Operator review evidence for portable CAO teams.

The operator selects a team composition. CAO checks an exact, immutable
approval statement rather than accepting a self-asserted approver field.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

SCHEMA = "cao.team.approval.v1"


class ApprovalError(ValueError):
    code = "TEAM_APPROVAL_REQUIRED"


def canonical(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=True) + "\n").encode("ascii")


def route_digest(config: Any) -> str:
    raw = config.raw if config is not None else {}
    relevant = {k: raw.get(k) for k in (
        "provider", "task", "agent_profiles", "registry_db", "runtime_id")}
    return hashlib.sha256(canonical(relevant)).hexdigest()


def policy(config: Any) -> dict[str, str]:
    raw = config.raw.get("team_approval") if config is not None else None
    if raw is None:
        return {"mode": "legacy"}
    if type(raw) is not dict or set(raw) - {
            "mode", "signer", "allowed_signers_file"
    } or raw.get("mode") not in ("legacy", "signed"):
        raise ApprovalError("invalid team approval configuration")
    if raw["mode"] == "legacy":
        if set(raw) != {"mode"}:
            raise ApprovalError("legacy mode must have no signing configuration")
        return {"mode": "legacy"}
    import os
    import re
    signer, file = raw.get("signer"), raw.get("allowed_signers_file")
    if (type(signer) is not str
            or not re.fullmatch(r"[A-Za-z0-9_.@+-]{1,96}", signer)
            or type(file) is not str or len(file) > 2048
            or not os.path.isabs(file) or os.path.normpath(file) != file):
        raise ApprovalError("signed approval requires an operator and absolute allowlist")
    return dict(raw)


def proposal(spec: Mapping[str, Any], config: Any) -> bytes:
    if not spec.get("initiative"):
        raise ApprovalError("team initiative is required for signed approval")
    blob = {
        "schema": SCHEMA,
        "initiative": spec["initiative"],
        "team_id": spec["team_id"],
        "spec_sha256": hashlib.sha256(canonical(spec)).hexdigest(),
        "routes_sha256": route_digest(config),
        "supersedes": spec.get("supersedes"),
        "change_reason": spec.get("change_reason"),
    }
    return canonical(blob)


def verify(spec: Mapping[str, Any], config: Any, signature: bytes) -> dict[str, str]:
    """Verify detached operator signature with a configured public allowlist."""
    import base64
    import os
    import shutil
    import subprocess
    import tempfile
    selected = policy(config)
    if selected["mode"] != "signed":
        raise ApprovalError("signed approval is not enabled")
    if (type(signature) is not bytes or not 0 < len(signature) <= 16384
            or not signature.startswith(b"-----BEGIN SSH SIGNATURE-----")):
        raise ApprovalError("invalid or missing detached operator signature")
    trusted = selected["allowed_signers_file"]
    try:
        st = os.stat(trusted)
        if os.path.islink(trusted) or not os.path.isfile(trusted) or st.st_mode & 0o022:
            raise ApprovalError("operator allowlist is not protected")
        binary = shutil.which("ssh-keygen")
        if binary is None:
            raise ApprovalError("OpenSSH verifier is unavailable")
        message = proposal(spec, config)
        with tempfile.TemporaryDirectory(prefix="cao-operator-verify-") as directory:
            sigfile = os.path.join(directory, "approval.sig")
            with open(sigfile, "wb") as stream:
                stream.write(signature)
            outcome = subprocess.run(
                [binary, "-Y", "verify", "-f", trusted, "-I", selected["signer"],
                 "-n", "cao-team", "-s", sigfile],
                input=message, capture_output=True, timeout=12, check=False)
        if outcome.returncode:
            raise ApprovalError("operator signature not accepted")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ApprovalError("operator signature verification unavailable") from exc
    return {
        "schema": SCHEMA,
        "signer": selected["signer"],
        "signature_b64": base64.b64encode(signature).decode("ascii"),
        "proposal_sha256": hashlib.sha256(message).hexdigest(),
        "routes_sha256": route_digest(config),
    }


def verify_saved(spec: Mapping[str, Any], config: Any, admission: Any) -> None:
    import base64
    if type(admission) is not dict or set(admission) != {
            "schema", "signer", "signature_b64",
            "proposal_sha256", "routes_sha256"}:
        raise ApprovalError("missing trusted team admission evidence")
    try:
        signature = base64.b64decode(admission["signature_b64"], validate=True)
    except (TypeError, ValueError) as exc:
        raise ApprovalError("invalid stored approval encoding") from exc
    if verify(spec, config, signature) != admission:
        raise ApprovalError("team approval, signer or provider routing changed")
