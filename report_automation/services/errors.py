from __future__ import annotations

from other_proof import OtherProofError


def other_proof_error_detail(exc: OtherProofError):
    replay_file_path = getattr(exc, "replay_file_path", None)
    if replay_file_path:
        return {"message": str(exc), "replay_file_path": replay_file_path}
    return str(exc)
