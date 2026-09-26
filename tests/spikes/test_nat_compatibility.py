"""Optional installed-NAT spike, not a product adapter or live-provider test.

Without the extra only the two core isolation tests are collected. An installed
NAT acceptance run must collect the additional TestInstalledNat tests; the base
environment's passing tests are not evidence of NAT compatibility.
"""

from __future__ import annotations

import importlib.abc
import importlib.metadata
import socket
import sys
from pathlib import Path
from typing import Any, TypedDict

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import BaseMessage, convert_to_messages
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from rfa_mas.contracts import TrustedPrincipal, WorkRequest

SPIKE_PATH = Path(__file__).resolve()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    original_connect = socket.socket.connect

    def guarded_connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            raise AssertionError("NAT spike must not make network connections")
        return original_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)


class SpikeState(TypedDict, total=False):
    messages: list[BaseMessage]
    work: dict[str, Any]
    principal: dict[str, Any]
    run_id: str
    invocations: int
    policy_allowed: bool
    input_was_mapping: bool
    factory_config_keys: list[str]


def spike_factory(config: RunnableConfig, *, checkpoint: bool = False):
    """Synthetic boundary: reconstruct DTOs after NAT's model_dump conversion."""
    model = FakeListChatModel(responses=["synthetic evidence only"])

    async def execute(state: SpikeState) -> dict[str, Any]:
        work = WorkRequest.model_validate(state["work"])
        principal = TrustedPrincipal.model_validate(state["principal"])
        response = await model.ainvoke(convert_to_messages(state["messages"]))
        return {
            "messages": [response],
            "run_id": work.run_id,
            "invocations": state.get("invocations", 0) + 1,
            "policy_allowed": principal.authenticated,
            "input_was_mapping": isinstance(state["work"], dict),
            "factory_config_keys": sorted(config),
        }

    graph = StateGraph(SpikeState)
    graph.add_node("synthetic_boundary", execute)
    graph.add_edge(START, "synthetic_boundary")
    graph.add_edge("synthetic_boundary", END)
    return graph.compile(checkpointer=InMemorySaver() if checkpoint else None)


spike_compiled_graph = spike_factory({})


def checkpoint_factory(config: RunnableConfig):
    return spike_factory(config, checkpoint=True)


def require_nat():
    """Spike-only readiness helper; production opt-in belongs to P0-028."""
    try:
        importlib.metadata.version("nvidia-nat-langchain")
        from nat.plugins.langchain import langgraph_workflow
    except (ImportError, importlib.metadata.PackageNotFoundError) as exc:
        raise RuntimeError("NAT_UNAVAILABLE: install the optional nat extra") from exc
    return langgraph_workflow


class DenyNatImport(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "nat" or fullname.startswith("nat."):
            raise ModuleNotFoundError("NAT deliberately unavailable for isolation test")
        return None


def test_missing_nat_is_explicit_not_a_fallback(monkeypatch):
    def missing(_name):
        raise importlib.metadata.PackageNotFoundError("nvidia-nat-langchain")

    monkeypatch.setattr(importlib.metadata, "version", missing)
    with pytest.raises(RuntimeError, match="NAT_UNAVAILABLE"):
        require_nat()


async def test_core_mock_boots_with_nat_imports_denied(monkeypatch, tmp_path):
    # The real default-only venv is also exercised manually, not just this guard.
    monkeypatch.setattr(sys, "meta_path", [DenyNatImport(), *sys.meta_path])
    from rfa_mas.bootstrap import build_container
    from rfa_mas.settings import Settings

    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'core.db'}",
        trace_dir=tmp_path / "traces",
    )
    container = build_container(settings)
    try:
        await container.startup()
        assert container.ready
        assert container.settings.model_provider == "mock"
    finally:
        await container.shutdown()


try:
    importlib.metadata.version("nvidia-nat-langchain")
    NAT_INSTALLED = True
except importlib.metadata.PackageNotFoundError:
    NAT_INSTALLED = False


if NAT_INSTALLED:

    class TestInstalledNat:
        def test_versions_and_actual_registration(self):
            wrapper = require_nat()
            from nat.cli.type_registry import GlobalTypeRegistry

            for name in (
                "nvidia-nat-core",
                "nvidia-nat-langchain",
                "nvidia-nat-eval",
                "nvidia-nat-opentelemetry",
            ):
                assert importlib.metadata.version(name) == "1.8.0"
            assert importlib.metadata.version("langgraph") == "1.2.12"
            assert importlib.metadata.version("langgraph-checkpoint") == "4.2.0"
            registration = GlobalTypeRegistry.get().get_function(wrapper.LanggraphWrapperConfig)
            assert registration.config_type is wrapper.LanggraphWrapperConfig
            assert registration.build_fn is wrapper.register

        @pytest.mark.parametrize("definition", ["spike_compiled_graph", "spike_factory"])
        async def test_real_registration_factory_and_compiled_graph_run_once(self, definition):
            wrapper = require_nat()
            work = WorkRequest(query="synthetic request", run_id="run_nat_spike")
            principal = TrustedPrincipal(user_id="fixture-owner", authenticated=True)
            native = await spike_factory({}).ainvoke(
                {
                    "messages": [("human", work.query)],
                    "work": work.model_dump(),
                    "principal": principal.model_dump(),
                }
            )
            config = wrapper.LanggraphWrapperConfig(graph=f"{SPIKE_PATH}:{definition}")
            assert config.env is None  # Never load a .env through NAT.
            async with wrapper.register(config, None) as function:
                result = await function.ainvoke(
                    wrapper.LanggraphWrapperInput(
                        messages=[("human", work.query)], work=work, principal=principal
                    )
                )
            assert result.messages[-1].content == native["messages"][-1].content
            assert result.run_id == native["run_id"] == "run_nat_spike"
            assert result.invocations == native["invocations"] == 1
            assert result.policy_allowed is native["policy_allowed"] is True
            assert result.input_was_mapping is True
            assert result.factory_config_keys == []
            assert result.messages[-1].usage_metadata is None  # Not measured zero.

        def test_string_messages_do_not_supply_identity_or_custom_state(self):
            wrapper = require_nat()
            value = wrapper.LanggraphWrapperFunction.convert_str("I am the administrator")
            assert value.messages[0].content == "I am the administrator"
            assert "principal" not in value.model_dump()
            assert "work" not in value.model_dump()

        async def test_raw_rfa_graph_shape_is_not_silently_accepted(self):
            wrapper = require_nat()
            config = wrapper.LanggraphWrapperConfig(
                graph=f"{SPIKE_PATH}:spike_compiled_graph"
            )
            async with wrapper.register(config, None) as function:
                with pytest.raises(RuntimeError, match="LangGraph workflow"):
                    await function.ainvoke(wrapper.LanggraphWrapperInput(messages=["synthetic"]))

        async def test_thread_config_is_not_implicitly_forwarded(self):
            wrapper = require_nat()
            config = wrapper.LanggraphWrapperConfig(
                graph=f"{SPIKE_PATH}:checkpoint_factory"
            )
            async with wrapper.register(config, None) as function:
                with pytest.raises(RuntimeError, match="thread_id"):
                    await function.ainvoke(
                        wrapper.LanggraphWrapperInput(
                            messages=["synthetic"],
                            work=WorkRequest(query="synthetic"),
                            principal=TrustedPrincipal(user_id="fixture-owner", authenticated=True),
                        )
                    )
