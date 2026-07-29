"""Per-graph LangSmith tracing-project routing for langgraph.json entrypoints."""

import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable

import langsmith as ls
from langgraph.graph.state import RunnableConfig
from langgraph.pregel import Pregel

# SPEEDBAY DEVIATION (OPE-15): factories must not bind the server's
# __pregel_runtime key; logic lives in the org layer.
from ..speedbay.runtime_compat import strip_server_runtime

AGENT_TRACING_PROJECT = "open-swe-agent"
REVIEW_TRACING_PROJECT = "open-swe-review"


def traced_graph_factory(
    factory: Callable[[RunnableConfig], Awaitable[Pregel]],
    project_name: str,
) -> Callable[[RunnableConfig], contextlib.AbstractAsyncContextManager[Pregel]]:
    @contextlib.asynccontextmanager
    async def entrypoint(config: RunnableConfig) -> AsyncIterator[Pregel]:
        # SPEEDBAY DEVIATION (OPE-15): every traced factory ends with
        # .with_config(config); strip langgraph-api's __pregel_runtime here,
        # at the single chokepoint, or graph draw/execution hits
        # _ReadRuntime.override. See agent/speedbay/runtime_compat.py.
        graph = await factory(strip_server_runtime(config))
        with ls.tracing_context(project_name=project_name):
            yield graph

    return entrypoint
