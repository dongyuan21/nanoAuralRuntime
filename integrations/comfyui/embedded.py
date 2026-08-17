"""Thin, dependency-free embedded-node bridge for the local ControlFoley route.

No object in this module is a ComfyUI authority.  Nodes translate UI values to
the adapter-owned local request, then delegate model lifecycle and invocation
to the already-established Runtime/ControlFoley contracts.
"""

from __future__ import annotations

import atexit
import os
import sys
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Iterator, Mapping, Optional, Protocol, Tuple, Union, cast

from nano_aural_runtime import (
    AdapterRegistry,
    CancellationToken,
    ExecutionContext,
    InvocationCancelledError,
    InvocationRejectedError,
    InvocationResult,
    NanoAuralRuntimeError,
    Runtime,
)
from nano_aural_runtime_controlfoley import (
    ControlFoleyAdapter,
    ControlFoleyLocalRequest,
    ControlFoleyTaskKind,
    controlfoley_local_deployment,
)

from .bootstrap import (
    OPERATOR_CONFIG_ENV,
    EmbeddedOperatorConfig,
    OperatorConfigError,
    operator_config_from_environment,
)


class ComfyUIEmbeddedError(RuntimeError):
    """Base error whose message is suitable for an embedded-node frontend."""


class ComfyUIValidationError(ComfyUIEmbeddedError):
    """The node values cannot be represented by the local task contract."""


class ComfyUIExecutionCancelled(ComfyUIEmbeddedError):
    """The host cancelled a locally executing invocation."""


class ComfyUIExecutionError(ComfyUIEmbeddedError):
    """A local Runtime invocation failed after valid node mapping."""


class ComfyUIOriginConflictError(ComfyUIEmbeddedError):
    """A preloaded upstream module does not match the selected local source."""


class CancellationSource(Protocol):
    """Minimal host cancellation seam; ComfyUI itself is never imported here."""

    def is_cancelled(self) -> bool: ...


class LocalInvocationRuntime(Protocol):
    """The small local-executor seam consumed by the embedded node."""

    def invoke(
        self, request: ControlFoleyLocalRequest, context: ExecutionContext
    ) -> InvocationResult: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class EmbeddedAudio:
    """Opaque audio value passed between embedded nodes without UI file authority."""

    name: str
    media_type: str
    content: bytes
    metadata: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("artifact name must be non-empty")
        if not isinstance(self.media_type, str) or not self.media_type:
            raise ValueError("artifact media_type must be non-empty")
        if not isinstance(self.content, bytes) or not self.content:
            raise ValueError("artifact content must be non-empty bytes")


def _module_file(module: ModuleType) -> Optional[Path]:
    value = getattr(module, "__file__", None)
    return Path(value).resolve() if isinstance(value, str) else None


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def assert_controlfoley_module_origins(
    source_dir: Path, modules: Optional[Mapping[str, ModuleType]] = None
) -> None:
    """Refuse a mixed official-plugin/upstream import before local Runtime load.

    A ComfyUI process may have already imported the official plugin's
    ``controlfoley`` modules.  Reusing those modules with another sealed local
    source would silently mix code origins, so the bridge emits a restartable,
    actionable refusal instead.
    """

    expected = Path(source_dir).resolve()
    candidates = modules if modules is not None else sys.modules
    for name, module in tuple(candidates.items()):
        if name not in {"controlfoley", "lib.flow_matching"} and not name.startswith(
            "controlfoley."
        ):
            continue
        if not isinstance(module, ModuleType):
            raise ComfyUIOriginConflictError(
                "loaded upstream module {0} is not a Python module; restart ComfyUI "
                "with one ControlFoley source".format(name)
            )
        origin = _module_file(module)
        if origin is None or not _is_within(origin, expected):
            actual = "unknown" if origin is None else str(origin)
            raise ComfyUIOriginConflictError(
                "ControlFoley module-origin conflict for {0}: loaded from {1}, "
                "but this embedded node is configured for {2}. Restart ComfyUI after "
                "selecting only one ControlFoley source.".format(name, actual, expected)
            )


class LocalControlFoleyRuntime:
    """Own one Runtime session for one explicitly configured embedded frontend.

    The owner below creates this object lazily and closes it explicitly when
    the embedding host tears down.  Node calls never construct independent
    adapters or sessions, preserving the adapter's single-flight lifecycle.
    """

    def __init__(self, manifest_path: Path, source_dir: Path, weights_dir: Path) -> None:
        assert_controlfoley_module_origins(source_dir)
        self._adapter = ControlFoleyAdapter()
        registry = AdapterRegistry()
        registry.register(self._adapter)
        self._runtime = Runtime(registry)
        self._session = self._runtime.load(
            controlfoley_local_deployment(
                self._adapter, Path(manifest_path), Path(source_dir), Path(weights_dir)
            )
        )
        self._closed = False

    def invoke(
        self, request: ControlFoleyLocalRequest, context: ExecutionContext
    ) -> InvocationResult:
        if self._closed:
            raise ComfyUIExecutionError("embedded ControlFoley runtime is closed")
        return self._runtime.invoke(
            self._session,
            request.to_invocation("comfyui-" + uuid.uuid4().hex),
            context,
        )

    def close(self) -> None:
        if not self._closed:
            self._runtime.unload(self._session)
            self._closed = True


RuntimeFactory = Callable[[], LocalInvocationRuntime]
CancellationSourceFactory = Callable[[], CancellationSource]


def _acquire_cancellation_source(
    factory: CancellationSourceFactory, timeout_seconds: float = 0.2
) -> CancellationSource:
    """Acquire and structurally validate a host source without trusting host latency."""

    finished = threading.Event()
    result: list[object] = []
    failure: list[BaseException] = []

    def acquire() -> None:
        try:
            result.append(factory())
        except BaseException as error:
            failure.append(error)
        finally:
            finished.set()

    threading.Thread(
        target=acquire,
        name="nano-aural-comfyui-cancel-factory",
        daemon=True,
    ).start()
    if not finished.wait(timeout_seconds):
        raise ComfyUIExecutionError(
            "ComfyUI cancellation source factory blocked; invocation was not started"
        )
    if failure:
        raise ComfyUIExecutionError(
            "ComfyUI cancellation source factory failed; invocation was not started"
        ) from failure[0]
    if len(result) != 1 or not callable(getattr(result[0], "is_cancelled", None)):
        raise ComfyUIExecutionError(
            "ComfyUI cancellation source factory returned an invalid source; "
            "invocation was not started"
        )
    return cast(CancellationSource, result[0])


class EmbeddedRuntimeOwner:
    """Lazy lifecycle with invocation leases and retryable, exclusive teardown."""

    def __init__(self, factory: RuntimeFactory) -> None:
        self._factory = factory
        self._runtime: Optional[LocalInvocationRuntime] = None
        self._condition = threading.Condition()
        self._active_invocations = 0
        self._closing = False
        self._closed = False
        self._unsafe_reason: Optional[str] = None

    @contextmanager
    def _lease(self) -> Iterator[LocalInvocationRuntime]:
        with self._condition:
            if self._closed:
                raise ComfyUIExecutionError("embedded ControlFoley runtime owner is closed")
            if self._closing:
                raise ComfyUIExecutionError("embedded ControlFoley runtime owner is closing")
            if self._unsafe_reason is not None:
                raise ComfyUIExecutionError(
                    "embedded ControlFoley runtime is unsafe and must be closed: {0}".format(
                        self._unsafe_reason
                    )
                )
            if self._runtime is None:
                self._runtime = self._factory()
            runtime = self._runtime
            self._active_invocations += 1
        try:
            yield runtime
        finally:
            with self._condition:
                self._active_invocations -= 1
                self._condition.notify_all()

    def invoke(
        self, request: ControlFoleyLocalRequest, context: ExecutionContext
    ) -> InvocationResult:
        with self._lease() as runtime:
            return runtime.invoke(request, context)

    def mark_unsafe(self, reason: str) -> None:
        with self._condition:
            if not self._closed:
                self._unsafe_reason = reason

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            while self._closing:
                self._condition.wait()
                if self._closed:
                    return
            self._closing = True
            while self._active_invocations:
                self._condition.wait()
            runtime = self._runtime
        try:
            if runtime is not None:
                runtime.close()
        except BaseException:
            with self._condition:
                # Retain the handle and an unsafe state so teardown can be retried.
                self._unsafe_reason = "previous unload failed"
                self._closing = False
                self._condition.notify_all()
            raise
        with self._condition:
            self._runtime = None
            self._closed = True
            self._closing = False
            self._condition.notify_all()


def _path_value(value: Optional[Union[Path, os.PathLike[str], str]], field: str) -> Optional[Path]:
    if value is None or value == "":
        return None
    if not isinstance(value, (Path, str, os.PathLike)):
        raise ComfyUIValidationError("{0} must be a local path string or Path".format(field))
    return Path(value)


def map_embedded_values(
    *,
    task: str,
    video: Optional[Union[Path, os.PathLike[str], str]] = None,
    reference_audio: Optional[Union[Path, os.PathLike[str], str]] = None,
    prompt: Optional[str] = None,
    seed: int = 42,
) -> ControlFoleyLocalRequest:
    """Translate primitive ComfyUI-like values into the adapter-owned schema."""

    try:
        return ControlFoleyLocalRequest(
            task=ControlFoleyTaskKind(task),
            video_path=_path_value(video, "video"),
            reference_audio_path=_path_value(reference_audio, "reference_audio"),
            prompt=prompt,
            seed=seed,
        )
    except (TypeError, ValueError) as error:
        raise ComfyUIValidationError(
            "invalid embedded ControlFoley inputs: {0}".format(error)
        ) from error


class _CancellationMonitor:
    """Fail-closed polling for a host cancellation source."""

    def __init__(
        self,
        source: CancellationSource,
        token: CancellationToken,
        timeout_seconds: float = 0.2,
    ) -> None:
        self._source = source
        self._token = token
        self._timeout_seconds = timeout_seconds
        self._stop = threading.Event()
        self._first_check = threading.Event()
        self._failure: Optional[BaseException] = None
        self._exit_reason: Optional[str] = None
        self._thread = threading.Thread(
            target=self._watch,
            name="nano-aural-comfyui-cancel",
            daemon=True,
        )

    def _watch(self) -> None:
        try:
            while True:
                cancelled = self._source.is_cancelled()
                if not isinstance(cancelled, bool):
                    raise TypeError("cancellation source must return bool")
                self._first_check.set()
                if cancelled:
                    self._token.cancel("ComfyUI execution cancelled")
                    self._exit_reason = "cancelled"
                    return
                if self._stop.wait(0.02):
                    self._exit_reason = "stopped"
                    return
        except BaseException as error:
            self._failure = error
            self._token.cancel("ComfyUI cancellation monitor failed")
            self._exit_reason = "failed"
            self._first_check.set()

    def start(self) -> None:
        self._thread.start()
        if not self._first_check.wait(self._timeout_seconds):
            self._stop.set()
            raise ComfyUIExecutionError(
                "ComfyUI cancellation precheck blocked; invocation was not started"
            )
        if self._failure is not None:
            raise ComfyUIExecutionError("ComfyUI cancellation precheck failed") from self._failure

    def stop_and_validate(self) -> None:
        self._stop.set()
        self._thread.join(timeout=self._timeout_seconds)
        if self._thread.is_alive():
            self._token.cancel("ComfyUI cancellation monitor blocked")
            raise ComfyUIExecutionError(
                "ComfyUI cancellation monitor did not stop; local runtime is unsafe"
            )
        if self._failure is not None:
            raise ComfyUIExecutionError("ComfyUI cancellation monitor failed") from self._failure
        if self._exit_reason not in {"stopped", "cancelled"}:
            raise ComfyUIExecutionError("ComfyUI cancellation monitor exited unexpectedly")


class ControlFoleyEmbeddedNode:
    """Custom-node facade that maps values and delegates to ``EmbeddedRuntimeOwner``."""

    CATEGORY = "audio/nanoAuralRuntime"
    FUNCTION = "generate"
    RETURN_TYPES = ("NANO_AURAL_AUDIO",)
    RETURN_NAMES = ("audio",)

    # These methods use only ComfyUI's declarative custom-node conventions;
    # importing or inheriting from ComfyUI is intentionally unnecessary.
    @classmethod
    def INPUT_TYPES(cls) -> Mapping[str, Mapping[str, Tuple[Any, ...]]]:
        return {
            "required": {
                "task": ([member.value for member in ControlFoleyTaskKind],),
                "seed": ("INT", {"default": 42, "min": 0, "max": 2147483647}),
            },
            "optional": {
                "video": ("STRING", {"default": ""}),
                "reference_audio": ("STRING", {"default": ""}),
                "prompt": ("STRING", {"default": "", "multiline": True}),
            },
        }

    def __init__(
        self,
        owner: Optional[EmbeddedRuntimeOwner] = None,
        cancellation_source_factory: Optional[CancellationSourceFactory] = None,
    ) -> None:
        self._owner = owner or _configured_owner()
        self._cancellation_source_factory = cancellation_source_factory

    def generate(
        self,
        task: str,
        seed: int = 42,
        video: Optional[Union[Path, os.PathLike[str], str]] = None,
        reference_audio: Optional[Union[Path, os.PathLike[str], str]] = None,
        prompt: Optional[str] = None,
        cancellation_source: Optional[CancellationSource] = None,
    ) -> Tuple[EmbeddedAudio]:
        if cancellation_source is None and self._cancellation_source_factory is not None:
            cancellation_source = _acquire_cancellation_source(self._cancellation_source_factory)
        if cancellation_source is not None and not callable(
            getattr(cancellation_source, "is_cancelled", None)
        ):
            raise ComfyUIExecutionError(
                "ComfyUI cancellation source is invalid; invocation was not started"
            )
        request = map_embedded_values(
            task=task,
            video=video,
            reference_audio=reference_audio,
            prompt=prompt,
            seed=seed,
        )
        token = CancellationToken()
        context = ExecutionContext(cancellation_token=token)
        monitor = (
            None
            if cancellation_source is None
            else _CancellationMonitor(cancellation_source, token)
        )
        if monitor is not None:
            monitor.start()
        try:
            token.raise_if_cancelled()
            try:
                result = self._owner.invoke(request, context)
            finally:
                if monitor is not None:
                    try:
                        monitor.stop_and_validate()
                    except ComfyUIExecutionError as monitor_error:
                        self._owner.mark_unsafe("cancellation monitoring failed")
                        try:
                            self._owner.close()
                        except BaseException as close_error:
                            raise ComfyUIExecutionError(
                                "cancellation monitoring failed and runtime teardown must be retried"
                            ) from close_error
                        raise monitor_error
            token.raise_if_cancelled()
            if len(result.artifacts) != 1:
                raise ComfyUIExecutionError(
                    "embedded ControlFoley invocation must return exactly one audio artifact"
                )
            artifact = result.artifacts[0]
            if not artifact.media_type.startswith("audio/"):
                raise ComfyUIExecutionError("embedded ControlFoley artifact is not audio")
            return (
                EmbeddedAudio(
                    name=artifact.name,
                    media_type=artifact.media_type,
                    content=artifact.content,
                    metadata=artifact.metadata,
                ),
            )
        except ComfyUIEmbeddedError:
            raise
        except InvocationCancelledError as error:
            raise ComfyUIExecutionCancelled("embedded ControlFoley invocation cancelled") from error
        except (InvocationRejectedError, TypeError, ValueError) as error:
            raise ComfyUIValidationError(
                "embedded ControlFoley rejected inputs: {0}".format(error)
            ) from error
        except (NanoAuralRuntimeError, OSError, RuntimeError) as error:
            raise ComfyUIExecutionError(
                "embedded ControlFoley invocation failed: {0}".format(error)
            ) from error


class EmbeddedAudioOutputNode:
    """Terminal UI sink that exposes only a bounded in-memory artifact summary."""

    CATEGORY = "audio/nanoAuralRuntime"
    FUNCTION = "summarize"
    OUTPUT_NODE = True
    RETURN_TYPES: Tuple[()] = ()

    @classmethod
    def INPUT_TYPES(cls) -> Mapping[str, Mapping[str, Tuple[Any, ...]]]:
        return {"required": {"audio": ("NANO_AURAL_AUDIO",)}}

    def summarize(self, audio: EmbeddedAudio) -> Mapping[str, Any]:
        if not isinstance(audio, EmbeddedAudio):
            raise ComfyUIValidationError("audio must be a NANO_AURAL_AUDIO value")
        # Adapter metadata may contain operator-only deployment information.
        # The output node therefore returns a strict allow-listed summary.
        return {
            "ui": {
                "nano_aural_audio": [
                    {
                        "name": audio.name,
                        "media_type": audio.media_type,
                        "size_bytes": len(audio.content),
                    }
                ]
            },
            "result": (),
        }


_configured_owner_lock = threading.Lock()
_configured_runtime_owner: Optional[EmbeddedRuntimeOwner] = None
_configured_cancellation_source_factory: Optional[CancellationSourceFactory] = None
OperatorRuntimeBuilder = Callable[[EmbeddedOperatorConfig], LocalInvocationRuntime]


def _default_operator_runtime_builder(config: EmbeddedOperatorConfig) -> LocalInvocationRuntime:
    return LocalControlFoleyRuntime(config.manifest_path, config.source_dir, config.weights_dir)


_operator_runtime_builder: OperatorRuntimeBuilder = _default_operator_runtime_builder


class _ComfyUIHostCancellationSource:
    def __init__(self, check_interrupted: Callable[[], None]) -> None:
        self._check_interrupted = check_interrupted

    def is_cancelled(self) -> bool:
        try:
            self._check_interrupted()
        except BaseException as error:
            if type(error).__name__ == "InterruptProcessingException":
                return True
            raise
        return False


def _default_host_cancellation_source_factory() -> CancellationSource:
    """Adapt an already-loaded ComfyUI host without importing it as a dependency."""

    module = sys.modules.get("comfy.model_management")
    callback = getattr(module, "throw_exception_if_processing_interrupted", None)
    if not callable(callback):
        raise ComfyUIExecutionError(
            "ComfyUI cancellation API is unavailable; invocation was not started"
        )
    return _ComfyUIHostCancellationSource(cast(Callable[[], None], callback))


def configure_embedded_runtime(
    factory: RuntimeFactory,
    cancellation_source_factory: Optional[CancellationSourceFactory] = None,
) -> EmbeddedRuntimeOwner:
    """Install one explicit local-runtime factory for ComfyUI node instances.

    An operator/bootstrap module must call this once with sealed local
    deployment paths.  Paths are deliberately not node inputs and no remote
    service contract is involved.
    """

    if not callable(factory):
        raise TypeError("factory must be callable")
    if cancellation_source_factory is not None and not callable(cancellation_source_factory):
        raise TypeError("cancellation_source_factory must be callable when supplied")
    global _configured_cancellation_source_factory, _configured_runtime_owner
    with _configured_owner_lock:
        if _configured_runtime_owner is not None:
            raise ComfyUIExecutionError("embedded runtime is already configured")
        _configured_runtime_owner = EmbeddedRuntimeOwner(factory)
        _configured_cancellation_source_factory = cancellation_source_factory
        return _configured_runtime_owner


def _configured_owner() -> EmbeddedRuntimeOwner:
    global _configured_runtime_owner
    with _configured_owner_lock:
        if _configured_runtime_owner is None:
            try:
                config = operator_config_from_environment()
            except OperatorConfigError as error:
                raise ComfyUIValidationError(
                    "embedded ControlFoley operator configuration is unavailable; set {0} "
                    "to a valid strict JSON config before ComfyUI discovers the node".format(
                        OPERATOR_CONFIG_ENV
                    )
                ) from error
            _configured_runtime_owner = EmbeddedRuntimeOwner(
                lambda: _operator_runtime_builder(config)
            )
        return _configured_runtime_owner


def _configured_cancellation_source() -> Optional[CancellationSourceFactory]:
    with _configured_owner_lock:
        return (
            _configured_cancellation_source_factory
            if _configured_cancellation_source_factory is not None
            else _default_host_cancellation_source_factory
        )


def teardown_embedded_runtime() -> None:
    """Unload the configured session; retain it when unload fails so callers can retry."""

    global _configured_cancellation_source_factory, _configured_runtime_owner
    with _configured_owner_lock:
        owner = _configured_runtime_owner
    if owner is None:
        return
    owner.close()
    with _configured_owner_lock:
        if _configured_runtime_owner is owner:
            _configured_runtime_owner = None
            _configured_cancellation_source_factory = None


class _ConfiguredControlFoleyEmbeddedNode(ControlFoleyEmbeddedNode):
    """ComfyUI-discovered node using the optional bootstrap cancellation seam."""

    def __init__(self) -> None:
        super().__init__(
            owner=_configured_owner(),
            cancellation_source_factory=_configured_cancellation_source(),
        )


NODE_CLASS_MAPPINGS = {
    "NanoAuralControlFoleyEmbedded": _ConfiguredControlFoleyEmbeddedNode,
    "NanoAuralAudioOutput": EmbeddedAudioOutputNode,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "NanoAuralControlFoleyEmbedded": "NanoAural ControlFoley (Local)",
    "NanoAuralAudioOutput": "NanoAural Audio Output",
}


atexit.register(teardown_embedded_runtime)
