from typing import Any

# SPEEDBAY DEVIATION: upstream ships its own workspace's team mapping here, and
# our Linear team "Open SWE" collided with theirs (routing to langchain-ai/open-swe,
# which the allowlist then rejected). Docs designate this file as deployer config.
# Unmapped teams fall back to DEFAULT_REPO_OWNER/DEFAULT_REPO_NAME
# (speedbay/warehouse). Per-comment `repo:owner/name` still overrides.
#
# OPE-45: the "Open SWE" team maps to our fork explicitly. Verify dispatches
# (OPE-39) carry no comment, so the per-comment override can never rescue
# them — without this entry every OPE verification searched the wrong repo.
LINEAR_TEAM_TO_REPO: dict[str, dict[str, Any] | dict[str, str]] = {
    "Open SWE": {"owner": "speedbay", "name": "open-swe"},
}
