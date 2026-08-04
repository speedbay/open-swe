import logging
from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.dashboard import user_mappings
from agent.webhooks import common


@pytest.fixture(autouse=True)
def langgraph_client(monkeypatch: pytest.MonkeyPatch) -> Generator[MagicMock, None, None]:
    user_mappings.prime_cache([])
    client = MagicMock()
    client.threads.get = AsyncMock(return_value={"metadata": {}})
    client.threads.update = AsyncMock()
    monkeypatch.setattr(common, "get_client", lambda url: client)
    yield client
    user_mappings.prime_cache([])


def attribution_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        message
        for message in caplog.messages
        if "Thread owner has no dashboard identity" in message
    ]


@pytest.mark.parametrize(
    ("thread_id", "user_email"),
    [
        ("thread-unmapped", "forge-bot@speedbay.com"),
        ("thread-unassigned", ""),
    ],
)
@pytest.mark.asyncio
async def test_missing_dashboard_identity_warns_once(
    caplog: pytest.LogCaptureFixture, thread_id: str, user_email: str
) -> None:
    caplog.set_level(logging.WARNING, logger=common.logger.name)

    await common.upsert_agent_thread_owner_metadata(
        thread_id, source="linear", user_email=user_email, github_login=""
    )

    assert attribution_warnings(caplog) == [
        "Thread owner has no dashboard identity: "
        f"thread_id={thread_id} source=linear user_email={user_email}"
    ]


@pytest.mark.asyncio
async def test_resolvable_email_does_not_warn(
    caplog: pytest.LogCaptureFixture, langgraph_client: MagicMock
) -> None:
    caplog.set_level(logging.WARNING, logger=common.logger.name)
    user_mappings.prime_cache(
        [
            {
                "github_login": "mapped-user",
                "work_email": "mapped@speedbay.com",
                "status": "active",
            }
        ]
    )

    await common.upsert_agent_thread_owner_metadata(
        "thread-mapped", source="linear", user_email="mapped@speedbay.com"
    )

    assert attribution_warnings(caplog) == []
    metadata = langgraph_client.threads.update.await_args.kwargs["metadata"]
    assert metadata["github_login"] == "mapped-user"
