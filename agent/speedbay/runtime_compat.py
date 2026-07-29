"""Keep langgraph-api's server runtime out of graph-bound configs (OPE-15).

SPEEDBAY org-layer file — upstream does not own it.

``langgraph_api.graph.get_graph()`` injects its ServerRuntime
(``langgraph_sdk.runtime._ReadRuntime`` / ``_ExecutionRuntime``) under
``config["configurable"]["__pregel_runtime"]`` before invoking a graph
factory. Open SWE's factories end with ``.with_config(config)``, which would
statically bind that factory-time object onto the compiled graph. langgraph
then finds it under the same key in pregel configs and calls
``Runtime.override()`` on it — a method the SDK server runtimes do not define
— yielding ``AttributeError: '_ReadRuntime' object has no attribute
'override'`` and a 500 on ``GET /assistants/{id}/graph`` (Studio "Failed to
preview graph", observed in OPE-1). Pregel constructs its own ``Runtime`` per
run; the server's key must never be pre-bound.

Every factory therefore binds ``strip_server_runtime(config)`` instead of the
raw config. The key name is written literally: it mirrors the private
``langgraph._internal._constants.CONFIG_KEY_RUNTIME``, and if upstream renames
it this helper degrades to a no-op instead of an import error.
"""

from __future__ import annotations

from langchain_core.runnables import RunnableConfig

_PREGEL_RUNTIME_KEY = "__pregel_runtime"


def strip_server_runtime(config: RunnableConfig) -> RunnableConfig:
    """Return ``config`` without the server-injected ``__pregel_runtime`` key.

    No-op (same object) when the key is absent; otherwise a shallow copy with
    only that key removed — the input is never mutated, and all other
    configurable entries pass through untouched.
    """
    configurable = config.get("configurable")
    if not configurable or _PREGEL_RUNTIME_KEY not in configurable:
        return config
    return {
        **config,
        "configurable": {
            key: value for key, value in configurable.items() if key != _PREGEL_RUNTIME_KEY
        },
    }
