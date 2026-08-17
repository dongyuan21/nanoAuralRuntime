"""CPU contracts and conditional hardware evidence for Roadmap Phase 5A."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping

import pytest  # pyright: ignore[reportMissingImports]

# The optional custom-node directory is intentionally outside the installed
# headless packages, matching ComfyUI's custom-node discovery layout.
ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import integrations.comfyui.embedded as embedded_module  # noqa: E402
from integrations.comfyui.bootstrap import (  # noqa: E402
    OPERATOR_CONFIG_ENV,
    OperatorConfigError,
    load_operator_config,
)
from integrations.comfyui.embedded import (  # noqa: E402
    NODE_CLASS_MAPPINGS,
    ComfyUIExecutionCancelled,
    ComfyUIExecutionError,
    ComfyUIOriginConflictError,
    ComfyUIValidationError,
    ControlFoleyEmbeddedNode,
    EmbeddedRuntimeOwner,
    assert_controlfoley_module_origins,
    map_embedded_values,
    teardown_embedded_runtime,
)
from nano_aural_runtime import (  # noqa: E402
    ExecutionContext,
    InvocationCancelledError,
    InvocationRejectedError,
    InvocationResult,
    ProducedArtifact,
)


class _RuntimeDouble:
    def __init__(self) -> None:
        self.requests = []
        self.closed = 0

    def invoke(self, request, context):  # type: ignore[no-untyped-def]
        self.requests.append((request, context))
        return InvocationResult(
            invocation_id="test",
            artifacts=(
                ProducedArtifact(
                    name="result.flac",
                    media_type="audio/flac",
                    content=b"FLAC-test",
                    metadata={"source": "runtime-double"},
                ),
            ),
        )

    def close(self) -> None:
        self.closed += 1


class _CloseFailsOnceRuntime(_RuntimeDouble):
    def close(self) -> None:
        self.closed += 1
        if self.closed == 1:
            raise RuntimeError("synthetic unload failure")


class _ConcurrentRuntime(_RuntimeDouble):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def invoke(self, request, context):  # type: ignore[no-untyped-def]
        self.started.set()
        assert self.release.wait(timeout=1)
        return super().invoke(request, context)


class _WaitForEventRuntime(_RuntimeDouble):
    def __init__(self, event: threading.Event) -> None:
        super().__init__()
        self._event = event

    def invoke(self, request, context):  # type: ignore[no-untyped-def]
        assert self._event.wait(timeout=1)
        return super().invoke(request, context)


class _RejectedRuntime(_RuntimeDouble):
    def invoke(self, request, context):  # type: ignore[no-untyped-def]
        raise InvocationRejectedError("sealed deployment rejected request")


class _CancellationSource:
    def __init__(self) -> None:
        self._cancelled = False
        self._lock = threading.Lock()

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True

    def is_cancelled(self) -> bool:
        with self._lock:
            return self._cancelled


class _ThrowingCancellationSource:
    def is_cancelled(self) -> bool:
        raise RuntimeError("synthetic cancellation source failure")


class _NonBooleanCancellationSource:
    def is_cancelled(self):  # type: ignore[no-untyped-def]
        return 1


class _InvalidCancellationSource:
    pass


class _FirstCheckBlockingCancellationSource:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def is_cancelled(self) -> bool:
        self.started.set()
        self.release.wait(timeout=2)
        return False


class _LateBlockingCancellationSource:
    def __init__(self) -> None:
        self.calls = 0
        self.blocked = threading.Event()
        self.release = threading.Event()

    def is_cancelled(self) -> bool:
        self.calls += 1
        if self.calls == 1:
            return False
        self.blocked.set()
        self.release.wait(timeout=2)
        return False


class _HostCancellationModule(ModuleType):
    def throw_exception_if_processing_interrupted(self) -> None:
        return None


class _BlockingRuntime(_RuntimeDouble):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()

    def invoke(self, request, context):  # type: ignore[no-untyped-def]
        self.started.set()
        while not context.cancellation_token.is_cancelled():
            threading.Event().wait(0.005)
        raise InvocationCancelledError("cancelled by test host")


def _node(runtime: _RuntimeDouble) -> ControlFoleyEmbeddedNode:
    return ControlFoleyEmbeddedNode(EmbeddedRuntimeOwner(lambda: runtime))


def test_node_maps_comfy_values_to_adapter_owned_local_request_and_audio() -> None:
    runtime = _RuntimeDouble()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        video = root / "video.mp4"
        video.write_bytes(b"video")
        output = _node(runtime).generate("TV2A", video=str(video), prompt="soft wind", seed=7)

    request, context = runtime.requests[0]
    assert request.task.value == "TV2A"
    assert request.video_path == video
    assert request.prompt == "soft wind"
    assert request.seed == 7
    assert not context.cancellation_token.is_cancelled()
    assert output[0].name == "result.flac"
    assert output[0].content == b"FLAC-test"


def test_mapping_preserves_the_exact_controlfoley_task_contract() -> None:
    with pytest.raises(ComfyUIValidationError, match="exact ControlFoley task contract"):
        map_embedded_values(task="T2A", video="unexpected.mp4", prompt="rain")
    with pytest.raises(ComfyUIValidationError, match="local path string or Path"):
        map_embedded_values(task="V2A", video=object())  # type: ignore[arg-type]


def test_node_converts_runtime_rejection_to_validation_error() -> None:
    with pytest.raises(ComfyUIValidationError, match="sealed deployment rejected request"):
        _node(_RejectedRuntime()).generate("T2A", prompt="rain")


def test_node_propagates_host_cancellation_into_the_runtime_token() -> None:
    runtime = _BlockingRuntime()
    source = _CancellationSource()
    error: list[BaseException] = []

    def execute() -> None:
        try:
            _node(runtime).generate("T2A", prompt="rain", cancellation_source=source)
        except BaseException as caught:
            error.append(caught)

    thread = threading.Thread(target=execute)
    thread.start()
    assert runtime.started.wait(timeout=1)
    source.cancel()
    thread.join(timeout=1)
    assert not thread.is_alive()
    assert len(error) == 1
    assert isinstance(error[0], ComfyUIExecutionCancelled)


def test_owner_lazily_owns_one_runtime_and_unloads_once() -> None:
    runtime = _RuntimeDouble()
    factories = 0

    def factory() -> _RuntimeDouble:
        nonlocal factories
        factories += 1
        return runtime

    owner = EmbeddedRuntimeOwner(factory)
    assert factories == 0
    request = map_embedded_values(task="T2A", prompt="rain")
    owner.invoke(request, ExecutionContext())
    owner.invoke(request, ExecutionContext())
    assert factories == 1
    owner.close()
    owner.close()
    assert runtime.closed == 1
    with pytest.raises(ComfyUIExecutionError, match="owner is closed"):
        owner.invoke(request, ExecutionContext())


def test_owner_retains_runtime_for_retry_after_close_failure() -> None:
    runtime = _CloseFailsOnceRuntime()
    owner = EmbeddedRuntimeOwner(lambda: runtime)
    owner.invoke(map_embedded_values(task="T2A", prompt="rain"), ExecutionContext())

    with pytest.raises(RuntimeError, match="synthetic unload failure"):
        owner.close()
    with pytest.raises(ComfyUIExecutionError, match="unsafe"):
        owner.invoke(map_embedded_values(task="T2A", prompt="rain"), ExecutionContext())
    owner.close()
    assert runtime.closed == 2


def test_owner_close_waits_for_concurrent_invoke_before_unload() -> None:
    runtime = _ConcurrentRuntime()
    owner = EmbeddedRuntimeOwner(lambda: runtime)
    request = map_embedded_values(task="T2A", prompt="rain")
    invoke_error: list[BaseException] = []
    close_error: list[BaseException] = []

    def invoke() -> None:
        try:
            owner.invoke(request, ExecutionContext())
        except BaseException as error:
            invoke_error.append(error)

    def close() -> None:
        try:
            owner.close()
        except BaseException as error:
            close_error.append(error)

    invocation = threading.Thread(target=invoke)
    invocation.start()
    assert runtime.started.wait(timeout=1)
    closer = threading.Thread(target=close)
    closer.start()
    time.sleep(0.03)
    assert runtime.closed == 0
    runtime.release.set()
    invocation.join(timeout=1)
    closer.join(timeout=1)
    assert not invocation.is_alive() and not closer.is_alive()
    assert invoke_error == [] and close_error == []
    assert runtime.closed == 1


def test_origin_conflict_has_an_actionable_refusal() -> None:
    module = ModuleType("controlfoley")
    module.__file__ = "/another/plugin/controlfoley/__init__.py"
    with pytest.raises(ComfyUIOriginConflictError, match="Restart ComfyUI"):
        assert_controlfoley_module_origins(Path("/locked/source"), {"controlfoley": module})


def test_origin_guard_accepts_same_source_and_ignores_unrelated_modules(tmp_path: Path) -> None:
    source = tmp_path / "locked-source"
    package = source / "controlfoley"
    package.mkdir(parents=True)
    module = ModuleType("controlfoley")
    module.__file__ = str(package / "__init__.py")
    unrelated = ModuleType("unrelated")
    unrelated.__file__ = "/another/plugin/unrelated.py"
    assert_controlfoley_module_origins(source, {"controlfoley": module, "unrelated": unrelated})


def test_cancellation_factory_and_precheck_fail_closed_without_invocation() -> None:
    runtime = _RuntimeDouble()

    def failed_factory():  # type: ignore[no-untyped-def]
        raise RuntimeError("synthetic factory failure")

    with pytest.raises(ComfyUIExecutionError, match="factory failed"):
        ControlFoleyEmbeddedNode(EmbeddedRuntimeOwner(lambda: runtime), failed_factory).generate(
            "T2A", prompt="rain"
        )
    with pytest.raises(ComfyUIExecutionError, match="precheck failed"):
        _node(runtime).generate(
            "T2A", prompt="rain", cancellation_source=_ThrowingCancellationSource()
        )
    assert runtime.requests == []


def test_cancellation_factory_timeout_none_and_invalid_fail_before_invocation() -> None:
    runtime = _RuntimeDouble()
    factory_started = threading.Event()
    factory_release = threading.Event()

    def blocked_factory():  # type: ignore[no-untyped-def]
        factory_started.set()
        factory_release.wait(timeout=2)
        return _CancellationSource()

    with pytest.raises(ComfyUIExecutionError, match="factory blocked"):
        ControlFoleyEmbeddedNode(EmbeddedRuntimeOwner(lambda: runtime), blocked_factory).generate(
            "T2A", prompt="rain"
        )
    assert factory_started.is_set()
    factory_release.set()

    for invalid_factory in (
        lambda: None,
        lambda: _InvalidCancellationSource(),
    ):
        with pytest.raises(ComfyUIExecutionError, match="invalid source"):
            ControlFoleyEmbeddedNode(
                EmbeddedRuntimeOwner(lambda: runtime),
                invalid_factory,  # type: ignore[arg-type]
            ).generate("T2A", prompt="rain")
    assert runtime.requests == []


def test_non_boolean_and_blocked_first_check_fail_before_invocation() -> None:
    runtime = _RuntimeDouble()
    with pytest.raises(ComfyUIExecutionError, match="precheck failed"):
        _node(runtime).generate(
            "T2A",
            prompt="rain",
            cancellation_source=_NonBooleanCancellationSource(),  # type: ignore[arg-type]
        )

    source = _FirstCheckBlockingCancellationSource()
    with pytest.raises(ComfyUIExecutionError, match="precheck blocked"):
        _node(runtime).generate("T2A", prompt="rain", cancellation_source=source)
    assert source.started.is_set()
    source.release.set()
    assert runtime.requests == []


def test_blocked_cancellation_monitor_never_returns_artifact_and_closes_runtime() -> None:
    source = _LateBlockingCancellationSource()
    runtime = _WaitForEventRuntime(source.blocked)
    owner = EmbeddedRuntimeOwner(lambda: runtime)
    with pytest.raises(ComfyUIExecutionError, match="monitor did not stop"):
        ControlFoleyEmbeddedNode(owner).generate("T2A", prompt="rain", cancellation_source=source)
    assert source.blocked.is_set()
    assert runtime.closed == 1
    with pytest.raises(ComfyUIExecutionError, match="owner is closed"):
        owner.invoke(map_embedded_values(task="T2A", prompt="rain"), ExecutionContext())
    source.release.set()


def test_host_discovers_executes_and_summarizes_nodes_from_operator_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    teardown_embedded_runtime()
    manifest = tmp_path / "deployment.json"
    source = tmp_path / "source"
    weights = tmp_path / "weights"
    manifest.write_text("{}", encoding="utf-8")
    source.mkdir()
    weights.mkdir()
    operator_config = tmp_path / "comfyui-operator.json"
    operator_config.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "manifest_path": str(manifest),
                "source_dir": str(source),
                "weights_dir": str(weights),
            }
        ),
        encoding="utf-8",
    )
    runtime = _RuntimeDouble()
    monkeypatch.setenv(OPERATOR_CONFIG_ENV, str(operator_config))
    monkeypatch.setattr(embedded_module, "_operator_runtime_builder", lambda config: runtime)
    host_cancellation = _HostCancellationModule("comfy.model_management")
    monkeypatch.setitem(sys.modules, "comfy.model_management", host_cancellation)

    producer_type = NODE_CLASS_MAPPINGS["NanoAuralControlFoleyEmbedded"]
    output_type = NODE_CLASS_MAPPINGS["NanoAuralAudioOutput"]
    assert producer_type.FUNCTION == "generate"
    assert output_type.OUTPUT_NODE is True
    assert {"manifest_path", "source_dir", "weights_dir"}.isdisjoint(
        producer_type.INPUT_TYPES()["required"] | producer_type.INPUT_TYPES()["optional"]
    )
    prompt = {
        "1": {
            "class_type": "NanoAuralControlFoleyEmbedded",
            "inputs": {"task": "T2A", "seed": 9, "prompt": "rain"},
        },
        "2": {
            "class_type": "NanoAuralAudioOutput",
            "inputs": {"audio": ["1", 0]},
        },
    }
    for node in prompt.values():
        node_type = NODE_CLASS_MAPPINGS[node["class_type"]]
        assert set(node_type.INPUT_TYPES()["required"]).issubset(node["inputs"])
    producer = producer_type()
    consumer = output_type()
    audio = producer.generate("T2A", prompt="rain", seed=9)[0]
    summary = consumer.summarize(audio)
    serialized = json.dumps(summary)
    assert summary["result"] == ()
    assert summary["ui"]["nano_aural_audio"][0]["size_bytes"] == len(b"FLAC-test")
    assert str(tmp_path) not in serialized
    assert "runtime-double" not in serialized
    teardown_embedded_runtime()
    assert runtime.closed == 1


def test_discovered_node_reports_missing_operator_config_actionably(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    teardown_embedded_runtime()
    monkeypatch.delenv(OPERATOR_CONFIG_ENV, raising=False)
    with pytest.raises(ComfyUIValidationError, match=OPERATOR_CONFIG_ENV):
        NODE_CLASS_MAPPINGS["NanoAuralControlFoleyEmbedded"]()


def test_operator_config_rejects_unknown_secret_without_echoing_it(tmp_path: Path) -> None:
    config = tmp_path / "operator.json"
    secret = "must-not-escape"
    config.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "manifest_path": str(tmp_path / "manifest.json"),
                "source_dir": str(tmp_path / "source"),
                "weights_dir": str(tmp_path / "weights"),
                "token": secret,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(OperatorConfigError) as caught:
        load_operator_config(config)
    assert secret not in str(caught.value)


def test_operator_config_rejects_boolean_schema_version(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    source = tmp_path / "source"
    weights = tmp_path / "weights"
    manifest.write_text("{}", encoding="utf-8")
    source.mkdir()
    weights.mkdir()
    config = tmp_path / "operator.json"
    config.write_text(
        json.dumps(
            {
                "schema_version": True,
                "manifest_path": str(manifest),
                "source_dir": str(source),
                "weights_dir": str(weights),
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(OperatorConfigError, match="schema_version"):
        load_operator_config(config)


def test_discovered_node_fails_closed_without_host_cancellation_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    teardown_embedded_runtime()
    runtime = _RuntimeDouble()
    embedded_module.configure_embedded_runtime(lambda: runtime)
    monkeypatch.delitem(sys.modules, "comfy.model_management", raising=False)
    try:
        node = NODE_CLASS_MAPPINGS["NanoAuralControlFoleyEmbedded"]()
        with pytest.raises(ComfyUIExecutionError, match="cancellation source factory failed"):
            node.generate("T2A", prompt="rain")
        assert runtime.requests == []
    finally:
        teardown_embedded_runtime()


def test_example_is_a_linked_standard_workflow_with_terminal_output() -> None:
    workflow = json.loads(
        (ROOT / "integrations/comfyui/examples/embedded_controlfoley_t2a.json").read_text(
            encoding="utf-8"
        )
    )
    nodes = {node["id"]: node for node in workflow["nodes"]}
    assert set(nodes) == {1, 2}
    assert all(node["type"] in NODE_CLASS_MAPPINGS for node in nodes.values())
    assert nodes[1]["widgets_values"] == ["T2A", 42, "", "", "gentle rain on a window"]
    assert workflow["links"] == [[1, 1, 0, 2, 0, "NANO_AURAL_AUDIO"]]
    assert NODE_CLASS_MAPPINGS[nodes[2]["type"]].OUTPUT_NODE is True


def test_headless_packages_import_without_the_optional_frontend() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import nano_aural_runtime; import nano_aural_runtime_controlfoley; "
            "import nano_aural_runtime_workers.controlfoley",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.gpu
@pytest.mark.controlfoley
def test_embedded_local_gpu_smoke_when_operator_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = os.environ.get("CONTROLFOLEY_COMFYUI_GPU_CONFIG")
    if not isinstance(raw, str):
        pytest.skip("CONTROLFOLEY_COMFYUI_GPU_CONFIG is not set; RTX 4090 UI smoke is deferred")
    assert raw is not None
    try:
        config: Mapping[str, Any] = json.loads(raw)
        required = {"operator_config_path", "task", "prompt"}
        if set(config) != required:
            pytest.fail(
                "CONTROLFOLEY_COMFYUI_GPU_CONFIG must contain exactly the documented fields"
            )
        task = config["task"]
        prompt = config["prompt"]
        if not isinstance(task, str) or not isinstance(prompt, str):
            pytest.fail("GPU smoke task and prompt must be strings")
        teardown_embedded_runtime()
        monkeypatch.setenv(OPERATOR_CONFIG_ENV, str(config["operator_config_path"]))
        host_cancellation = _HostCancellationModule("comfy.model_management")
        monkeypatch.setitem(sys.modules, "comfy.model_management", host_cancellation)
        try:
            producer = NODE_CLASS_MAPPINGS["NanoAuralControlFoleyEmbedded"]()
            consumer = NODE_CLASS_MAPPINGS["NanoAuralAudioOutput"]()
            output = producer.generate(task, prompt=prompt)
            summary = consumer.summarize(output[0])
        finally:
            teardown_embedded_runtime()
    except (OSError, RuntimeError, ValueError, TypeError) as error:
        pytest.fail("embedded ControlFoley GPU smoke failed: {0}".format(error))
    assert output[0].media_type == "audio/flac"
    assert output[0].content
    assert summary["result"] == ()
