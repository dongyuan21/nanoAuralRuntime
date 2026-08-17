"""Headless upstream-parity ControlFoley adapter; no ComfyUI or service imports."""

from __future__ import annotations

import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Protocol, Tuple

from nano_aural_runtime import (
    CacheReport,
    ExecutionContext,
    InvocationCancelledError,
    InvocationRejectedError,
    InvocationResult,
    ModelDeployment,
    ModelDescriptor,
    ModelInvocation,
    ModelSession,
    ProducedArtifact,
)

from .baseline import (
    ControlFoleyDeploymentManifest,
    SchemaValidationError,
    detect_gpu_prerequisites,
    fingerprint_file,
    load_json,
    manifest_sha256,
)
from .cache import (
    EXPERIMENTAL_L0_L1_CACHE_MODE,
    ControlFoleyCacheInputDrift,
    ControlFoleyCachePolicy,
    ControlFoleyCacheTransaction,
    ControlFoleyL0L1Cache,
    cached_configuration_keys,
)
from .condition_cache import (
    EXPERIMENTAL_L2_CACHE_MODE,
    ControlFoleyConditionCache,
    ControlFoleyConditionCachePolicy,
    ControlFoleyConditionCacheTransaction,
    l2_cached_configuration_keys,
    merge_controlfoley_cache_reports,
)
from .profile import (
    ControlFoleyProfileBinding,
    ControlFoleyProfileLevel,
    ControlFoleyProfiler,
    ControlFoleyProfileRecorder,
)
from .staged import (
    CONTROLFOLEY_STAGE_ORDER,
    ControlFoleyComparisonEvidence,
    ControlFoleyConditionFeature,
    ControlFoleyStagedBackend,
    ControlFoleyStagedPath,
    ControlFoleyStageValue,
    controlfoley_invocation_fingerprint,
    validate_staged_backend_id,
)
from .tasks import (
    EXPERIMENTAL_STAGED_OPERATION,
    UPSTREAM_PARITY_OPERATION,
    ControlFoleyLocalRequest,
)

_CONFIGURATION_KEYS = {
    "manifest_path",
    "deployment_manifest_sha256",
    "source_dir",
    "weights_dir",
    "upstream_repository",
    "source_revision",
    "variant",
    "precision",
    "checkpoint_sha256",
}
_STAGED_CONFIGURATION_KEYS = _CONFIGURATION_KEYS | {
    "execution_path",
    "staged_backend_id",
    "staged_deployment_fingerprint",
}
_CACHED_STAGED_CONFIGURATION_KEYS = _STAGED_CONFIGURATION_KEYS | set(cached_configuration_keys())
_L2_CACHED_STAGED_CONFIGURATION_KEYS = _CACHED_STAGED_CONFIGURATION_KEYS | set(
    l2_cached_configuration_keys()
)
_VALIDATION_REPORT_KEYS = {
    "deployment_manifest_sha256",
    "source_revision",
    "variant",
    "precision",
    "checkpoint_sha256",
    "checkpoint_size_bytes",
    "external_weight_sha256",
}
_PROVENANCE_KEYS = {
    "sha256",
    "size_bytes",
    "source_revision",
    "deployment_manifest_sha256",
    "variant",
    "precision",
    "checkpoint_sha256",
    "format",
    "worker",
}


class UpstreamRunner(Protocol):
    def validate(self, configuration: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def invoke(
        self,
        configuration: Mapping[str, Any],
        request: ControlFoleyLocalRequest,
        context: ExecutionContext,
    ) -> Tuple[bytes, Mapping[str, Any]]: ...


class UpstreamParityRunner:
    """Runs the pinned original demo once in the Phase 2A isolated worker."""

    def __init__(
        self,
        popen_factory: Callable[..., Any] = subprocess.Popen,
        poll_interval_seconds: float = 0.05,
        terminate_wait_seconds: float = 5.0,
    ) -> None:
        self._popen_factory = popen_factory
        self._poll_interval_seconds = poll_interval_seconds
        self._terminate_wait_seconds = terminate_wait_seconds

    def validate(self, configuration: Mapping[str, Any]) -> Mapping[str, Any]:
        manifest, source_dir, weights_dir = _deployment_material(configuration)
        prerequisites = detect_gpu_prerequisites(source_dir, weights_dir, manifest)
        if not prerequisites.available:
            raise InvocationRejectedError(
                "ControlFoley deployment is not ready: {0}".format("; ".join(prerequisites.reasons))
            )
        _guard_upstream_module_origins(source_dir)
        report = {
            "deployment_manifest_sha256": manifest_sha256(manifest.to_dict()),
            "source_revision": manifest.source_revision,
            "variant": manifest.variant,
            "precision": manifest.precision,
            "checkpoint_sha256": manifest.checkpoint.sha256,
            "checkpoint_size_bytes": manifest.checkpoint.size_bytes,
            "external_weight_sha256": {
                item.relative_path: item.fingerprint.sha256 for item in manifest.external_weights
            },
        }
        _validate_validation_report(report, configuration)
        return report

    def invoke(
        self,
        configuration: Mapping[str, Any],
        request: ControlFoleyLocalRequest,
        context: ExecutionContext,
    ) -> Tuple[bytes, Mapping[str, Any]]:
        manifest, source_dir, weights_dir = _deployment_material(configuration)
        context.cancellation_token.raise_if_cancelled()
        # Validate again immediately before start: source/weights might change
        # after load, and every execution needs fresh provenance evidence.
        self.validate(configuration)
        with tempfile.TemporaryDirectory(prefix="nano-aural-controlfoley-output-") as directory:
            output_dir = Path(directory) / "output"
            report_path = Path(directory) / "worker.json"
            command = [
                sys.executable,
                "-m",
                "nano_aural_runtime_controlfoley.upstream_worker",
                "--source-dir",
                str(source_dir.resolve()),
                "--weights-dir",
                str(weights_dir.resolve()),
                "--report",
                str(report_path.resolve()),
                "--",
                "--variant",
                manifest.variant,
                "--duration",
                str(request.duration_seconds),
                "--cfg_strength",
                "4.5",
                "--num_steps",
                "25",
                "--output",
                str(output_dir.resolve()),
                "--seed",
                str(request.seed),
            ]
            if request.video_path is not None:
                command.extend(("--video", str(request.video_path.resolve())))
            if request.reference_audio_path is not None:
                command.extend(("--audio", str(request.reference_audio_path.resolve())))
            if request.prompt is not None:
                command.extend(("--prompt", request.prompt))
            process = self._popen_factory(
                command,
                cwd=source_dir.resolve(),
                shell=False,
                **_popen_isolation_kwargs(),
            )
            try:
                while process.poll() is None:
                    if context.cancellation_token.is_cancelled():
                        _cancel_process(process, self._terminate_wait_seconds)
                        raise InvocationCancelledError("ControlFoley upstream invocation cancelled")
                    time.sleep(self._poll_interval_seconds)
                if process.returncode != 0:
                    raise InvocationRejectedError("ControlFoley upstream demo failed")
            finally:
                if process.poll() is None:
                    _kill_process(process, self._terminate_wait_seconds)
            output = _single_flac_output(output_dir)
            content = output.read_bytes()
            fingerprint = fingerprint_file(output)
            report = _load_worker_report(report_path)
            return content, {
                "sha256": fingerprint.sha256,
                "size_bytes": fingerprint.size_bytes,
                "source_revision": manifest.source_revision,
                "deployment_manifest_sha256": manifest_sha256(manifest.to_dict()),
                "variant": manifest.variant,
                "precision": manifest.precision,
                "checkpoint_sha256": manifest.checkpoint.sha256,
                "format": "flac",
                "worker": report,
            }


def _deployment_material(
    configuration: Mapping[str, Any],
) -> Tuple[ControlFoleyDeploymentManifest, Path, Path]:
    if set(configuration) != _CONFIGURATION_KEYS:
        raise InvocationRejectedError(
            "ControlFoley deployment configuration has missing or unexpected fields"
        )
    manifest_path = Path(str(configuration["manifest_path"])).resolve()
    source_dir = Path(str(configuration["source_dir"])).resolve()
    weights_dir = Path(str(configuration["weights_dir"])).resolve()
    try:
        manifest = ControlFoleyDeploymentManifest.from_dict(load_json(manifest_path))
    except (OSError, SchemaValidationError, ValueError) as error:
        raise InvocationRejectedError("ControlFoley deployment manifest is invalid") from error
    expected = {
        "deployment_manifest_sha256": manifest_sha256(manifest.to_dict()),
        "upstream_repository": manifest.upstream_repository,
        "source_revision": manifest.source_revision,
        "variant": manifest.variant,
        "precision": manifest.precision,
        "checkpoint_sha256": manifest.checkpoint.sha256,
    }
    if manifest.checkpoint.status != "verified" or manifest.checkpoint.sha256 is None:
        raise InvocationRejectedError("ControlFoley checkpoint fingerprint must be verified")
    if any(weight.fingerprint.status != "verified" for weight in manifest.external_weights):
        raise InvocationRejectedError("ControlFoley external weight fingerprints must be verified")
    if any(configuration[name] != value for name, value in expected.items()):
        raise InvocationRejectedError(
            "ControlFoley deployment configuration is not bound to its manifest"
        )
    return manifest, source_dir, weights_dir


def controlfoley_deployment_configuration(
    manifest_path: Path, source_dir: Path, weights_dir: Path
) -> Mapping[str, str]:
    """Build a sealed adapter configuration from a real P2A deployment lock."""

    try:
        manifest = ControlFoleyDeploymentManifest.from_dict(load_json(manifest_path))
    except (OSError, SchemaValidationError, ValueError) as error:
        raise InvocationRejectedError("ControlFoley deployment manifest is invalid") from error
    if manifest.checkpoint.status != "verified" or manifest.checkpoint.sha256 is None:
        raise InvocationRejectedError("ControlFoley checkpoint fingerprint must be verified")
    if any(weight.fingerprint.status != "verified" for weight in manifest.external_weights):
        raise InvocationRejectedError("ControlFoley external weight fingerprints must be verified")
    return {
        "manifest_path": str(manifest_path.resolve()),
        "deployment_manifest_sha256": manifest_sha256(manifest.to_dict()),
        "source_dir": str(source_dir.resolve()),
        "weights_dir": str(weights_dir.resolve()),
        "upstream_repository": manifest.upstream_repository,
        "source_revision": manifest.source_revision,
        "variant": manifest.variant,
        "precision": manifest.precision,
        "checkpoint_sha256": manifest.checkpoint.sha256,
    }


def controlfoley_local_deployment(
    adapter: "ControlFoleyAdapter", manifest_path: Path, source_dir: Path, weights_dir: Path
) -> ModelDeployment:
    """Create the generic Core deployment without exposing adapter fields to Core."""

    configuration = controlfoley_deployment_configuration(manifest_path, source_dir, weights_dir)
    return ModelDeployment(
        deployment_id="controlfoley-local-fp32",
        descriptor=adapter.descriptor,
        fingerprint=configuration["deployment_manifest_sha256"],
        configuration=configuration,
    )


def controlfoley_staged_deployment_configuration(
    manifest_path: Path,
    source_dir: Path,
    weights_dir: Path,
    backend_id: str,
) -> Mapping[str, str]:
    """Build a separately sealed, explicitly experimental staged deployment."""

    backend_id = validate_staged_backend_id(backend_id)
    configuration = dict(
        controlfoley_deployment_configuration(manifest_path, source_dir, weights_dir)
    )
    configuration.update(
        {
            "execution_path": EXPERIMENTAL_STAGED_OPERATION,
            "staged_backend_id": backend_id,
        }
    )
    configuration["staged_deployment_fingerprint"] = _staged_deployment_fingerprint(configuration)
    return configuration


def controlfoley_staged_local_deployment(
    adapter: "ControlFoleyAdapter",
    manifest_path: Path,
    source_dir: Path,
    weights_dir: Path,
    backend_id: str,
) -> ModelDeployment:
    configuration = controlfoley_staged_deployment_configuration(
        manifest_path, source_dir, weights_dir, backend_id
    )
    fingerprint = configuration["staged_deployment_fingerprint"]
    return ModelDeployment(
        deployment_id="controlfoley-local-experimental-staged-" + fingerprint[:12],
        descriptor=adapter.descriptor,
        fingerprint=fingerprint,
        configuration=configuration,
    )


def controlfoley_staged_cached_deployment_configuration(
    manifest_path: Path,
    source_dir: Path,
    weights_dir: Path,
    backend_id: str,
    policy: ControlFoleyCachePolicy,
) -> Mapping[str, str]:
    """Build the separately sealed, explicit experimental L0/L1 deployment."""

    if not isinstance(policy, ControlFoleyCachePolicy):
        raise TypeError("policy must be ControlFoleyCachePolicy")
    configuration = dict(
        controlfoley_staged_deployment_configuration(
            manifest_path, source_dir, weights_dir, backend_id
        )
    )
    configuration.update(policy.configuration())
    configuration["staged_deployment_fingerprint"] = _staged_deployment_fingerprint(configuration)
    return configuration


def controlfoley_staged_cached_local_deployment(
    adapter: "ControlFoleyAdapter",
    manifest_path: Path,
    source_dir: Path,
    weights_dir: Path,
    backend_id: str,
    policy: ControlFoleyCachePolicy,
) -> ModelDeployment:
    configuration = controlfoley_staged_cached_deployment_configuration(
        manifest_path, source_dir, weights_dir, backend_id, policy
    )
    fingerprint = configuration["staged_deployment_fingerprint"]
    return ModelDeployment(
        deployment_id="controlfoley-local-experimental-l0-l1-" + fingerprint[:12],
        descriptor=adapter.descriptor,
        fingerprint=fingerprint,
        configuration=configuration,
    )


def controlfoley_staged_l2_cached_deployment_configuration(
    manifest_path: Path,
    source_dir: Path,
    weights_dir: Path,
    backend_id: str,
    base_policy: ControlFoleyCachePolicy,
    condition_policy: ControlFoleyConditionCachePolicy,
) -> Mapping[str, str]:
    """Build an independent, explicitly selected L0/L1/L2 deployment seal."""

    if not isinstance(condition_policy, ControlFoleyConditionCachePolicy):
        raise TypeError("condition_policy must be ControlFoleyConditionCachePolicy")
    condition_policy.assert_operator_separation(source_dir, weights_dir)
    configuration = dict(
        controlfoley_staged_cached_deployment_configuration(
            manifest_path, source_dir, weights_dir, backend_id, base_policy
        )
    )
    configuration.update(condition_policy.configuration())
    configuration["staged_deployment_fingerprint"] = _staged_deployment_fingerprint(configuration)
    return configuration


def controlfoley_staged_l2_cached_local_deployment(
    adapter: "ControlFoleyAdapter",
    manifest_path: Path,
    source_dir: Path,
    weights_dir: Path,
    backend_id: str,
    base_policy: ControlFoleyCachePolicy,
    condition_policy: ControlFoleyConditionCachePolicy,
) -> ModelDeployment:
    configuration = controlfoley_staged_l2_cached_deployment_configuration(
        manifest_path,
        source_dir,
        weights_dir,
        backend_id,
        base_policy,
        condition_policy,
    )
    fingerprint = configuration["staged_deployment_fingerprint"]
    return ModelDeployment(
        deployment_id="controlfoley-local-experimental-l2-" + fingerprint[:12],
        descriptor=adapter.descriptor,
        fingerprint=fingerprint,
        configuration=configuration,
    )


def _staged_deployment_fingerprint(configuration: Mapping[str, Any]) -> str:
    validate_staged_backend_id(configuration.get("staged_backend_id"))
    identity = {
        "schema_version": 1,
        "execution_path": configuration.get("execution_path"),
        "staged_backend_id": configuration.get("staged_backend_id"),
        "deployment_manifest_sha256": configuration.get("deployment_manifest_sha256"),
        "upstream_repository": configuration.get("upstream_repository"),
        "source_revision": configuration.get("source_revision"),
        "variant": configuration.get("variant"),
        "precision": configuration.get("precision"),
        "checkpoint_sha256": configuration.get("checkpoint_sha256"),
    }
    cache_keys = {key for key in configuration if key.startswith("cache_")}
    if cache_keys:
        if cache_keys != set(cached_configuration_keys()):
            raise ValueError("cached deployment has incomplete cache identity")
        identity["cache"] = {key: configuration.get(key) for key in sorted(cache_keys)}
    l2_cache_keys = {key for key in configuration if key.startswith("l2_cache_")}
    if l2_cache_keys:
        if l2_cache_keys != set(l2_cached_configuration_keys()):
            raise ValueError("L2 cached deployment has incomplete cache identity")
        identity["l2_cache"] = {key: configuration.get(key) for key in sorted(l2_cache_keys)}
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _validate_staged_deployment(session: ModelSession) -> None:
    configuration = session.deployment.configuration
    if set(configuration) not in (
        _STAGED_CONFIGURATION_KEYS,
        _CACHED_STAGED_CONFIGURATION_KEYS,
        _L2_CACHED_STAGED_CONFIGURATION_KEYS,
    ):
        raise InvocationRejectedError(
            "staged deployment configuration has missing or unexpected fields"
        )
    if configuration["execution_path"] != EXPERIMENTAL_STAGED_OPERATION:
        raise InvocationRejectedError("staged deployment is not explicitly selected")
    try:
        validate_staged_backend_id(configuration["staged_backend_id"])
    except ValueError as error:
        raise InvocationRejectedError("staged deployment backend id is unsafe") from error
    if set(configuration) in (
        _CACHED_STAGED_CONFIGURATION_KEYS,
        _L2_CACHED_STAGED_CONFIGURATION_KEYS,
    ):
        try:
            policy = ControlFoleyCachePolicy(
                preprocess_version=configuration["cache_preprocess_version"],
                code_version=configuration["cache_code_version"],
            )
            policy.assert_configuration(configuration)
        except (TypeError, ValueError) as error:
            raise InvocationRejectedError("cached deployment seal is invalid") from error
    if set(configuration) == _L2_CACHED_STAGED_CONFIGURATION_KEYS:
        expected_l2 = {
            "l2_cache_mode": EXPERIMENTAL_L2_CACHE_MODE,
            "l2_cache_codec_version": configuration.get("l2_cache_codec_version"),
            "l2_cache_schema_version": configuration.get("l2_cache_schema_version"),
        }
        try:
            for name in ("l2_cache_codec_version", "l2_cache_schema_version"):
                value = expected_l2[name]
                if (
                    not isinstance(value, str)
                    or not value
                    or len(value) > 64
                    or not value.isascii()
                    or any(not (character.isalnum() or character in "._-") for character in value)
                ):
                    raise ValueError("unsafe L2 version")
            expected_fingerprint = manifest_sha256(
                {
                    "schema_version": 1,
                    "mode": expected_l2["l2_cache_mode"],
                    "codec_version": expected_l2["l2_cache_codec_version"],
                    "condition_schema_version": expected_l2["l2_cache_schema_version"],
                }
            )
            if configuration.get("l2_cache_policy_fingerprint") != expected_fingerprint:
                raise ValueError("L2 cache policy fingerprint mismatch")
        except (TypeError, ValueError) as error:
            raise InvocationRejectedError("L2 cached deployment seal is invalid") from error
    expected = _staged_deployment_fingerprint(configuration)
    if (
        configuration["staged_deployment_fingerprint"] != expected
        or session.deployment.fingerprint != expected
    ):
        raise InvocationRejectedError("staged deployment fingerprint is not sealed")


def _single_flac_output(output_dir: Path) -> Path:
    candidates = sorted(item for item in output_dir.rglob("*.flac") if item.is_file())
    if len(candidates) != 1:
        raise InvocationRejectedError("upstream demo did not produce exactly one FLAC artifact")
    return candidates[0]


def _load_worker_report(path: Path) -> Mapping[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError) as error:
        raise InvocationRejectedError("upstream worker report is unavailable") from error
    if not isinstance(value, Mapping) or set(value) != {
        "wall_time_seconds",
        "peak_allocated_bytes",
        "peak_reserved_bytes",
    }:
        raise InvocationRejectedError("upstream worker report has missing or unexpected fields")
    wall = value["wall_time_seconds"]
    if (
        isinstance(wall, bool)
        or not isinstance(wall, (int, float))
        or not math.isfinite(wall)
        or wall <= 0
    ):
        raise InvocationRejectedError("upstream worker wall time must be finite and positive")
    for name in ("peak_allocated_bytes", "peak_reserved_bytes"):
        item = value[name]
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise InvocationRejectedError("upstream worker peaks must be non-negative integers")
    return dict(value)


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _guard_upstream_module_origins(source_dir: Path) -> None:
    """Reject conflicting pre-imported upstream modules rather than shadowing them."""

    for name, module in tuple(sys.modules.items()):
        if (
            name != "controlfoley"
            and not name.startswith("controlfoley.")
            and name != "lib.flow_matching"
        ):
            continue
        module_file = getattr(module, "__file__", None)
        if not isinstance(module_file, str) or not _path_is_within(Path(module_file), source_dir):
            raise InvocationRejectedError(
                "loaded upstream module {0} does not originate from the locked source directory".format(
                    name
                )
            )


def _popen_isolation_kwargs() -> Mapping[str, Any]:
    return {"start_new_session": True} if os.name == "posix" else {}


def _signal_process_group(process: Any, signum: int, fallback: str) -> None:
    if os.name == "posix" and isinstance(getattr(process, "pid", None), int):
        try:
            os.killpg(os.getpgid(process.pid), signum)
            return
        except OSError:
            pass
    getattr(process, fallback)()


def _kill_process(process: Any, timeout: float) -> None:
    _signal_process_group(process, signal.SIGKILL, "kill")
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise InvocationRejectedError("ControlFoley child did not exit after KILL") from error


def _cancel_process(process: Any, timeout: float) -> None:
    _signal_process_group(process, signal.SIGTERM, "terminate")
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process(process, timeout)


def _validate_validation_report(
    report: Mapping[str, Any], configuration: Mapping[str, Any]
) -> None:
    if set(report) != _VALIDATION_REPORT_KEYS:
        raise InvocationRejectedError("runner validation report has missing or unexpected fields")
    expected = {
        "deployment_manifest_sha256": configuration["deployment_manifest_sha256"],
        "source_revision": configuration["source_revision"],
        "variant": configuration["variant"],
        "precision": configuration["precision"],
        "checkpoint_sha256": configuration["checkpoint_sha256"],
    }
    if any(report[name] != value for name, value in expected.items()):
        raise InvocationRejectedError(
            "runner validation report does not bind the sealed deployment"
        )
    if not isinstance(report["checkpoint_size_bytes"], int) or report["checkpoint_size_bytes"] < 0:
        raise InvocationRejectedError("runner validation report has invalid checkpoint size")
    weights = report["external_weight_sha256"]
    if (
        not isinstance(weights, Mapping)
        or not weights
        or any(
            not isinstance(path, str) or not isinstance(digest, str)
            for path, digest in weights.items()
        )
    ):
        raise InvocationRejectedError(
            "runner validation report has invalid external weight evidence"
        )


class ControlFoleyAdapter:
    """Direct parity adapter plus a separately sealed experimental staged path."""

    _descriptor = ModelDescriptor(
        adapter_id="controlfoley",
        model_id="xiaomi-research/controlfoley",
        version="upstream-parity-1",
        capabilities={
            "operation": UPSTREAM_PARITY_OPERATION,
            "default_operation": UPSTREAM_PARITY_OPERATION,
            "experimental_operations": (EXPERIMENTAL_STAGED_OPERATION,),
            "comfyui": False,
            "staged": "explicit_opt_in_operator_backend",
            "profile_levels": tuple(level.value for level in ControlFoleyProfileLevel),
            "profile_default": ControlFoleyProfileLevel.OFF.value,
            "cache_modes": (
                "off",
                EXPERIMENTAL_L0_L1_CACHE_MODE,
                EXPERIMENTAL_L2_CACHE_MODE,
            ),
            "cache_default": "off",
        },
    )

    def __init__(
        self,
        runner: Optional[UpstreamRunner] = None,
        staged_backend: Optional[ControlFoleyStagedBackend] = None,
        profiler: Optional[ControlFoleyProfiler] = None,
        cache: Optional[ControlFoleyL0L1Cache] = None,
        condition_cache: Optional[ControlFoleyConditionCache] = None,
    ) -> None:
        self._runner: UpstreamRunner = runner or UpstreamParityRunner()
        self._staged_path = (
            None if staged_backend is None else ControlFoleyStagedPath(staged_backend)
        )
        self._sessions: Dict[str, Mapping[str, Any]] = {}
        self._session_operations: Dict[str, str] = {}
        self._session_cache_enabled: Dict[str, bool] = {}
        self._session_l2_cache_enabled: Dict[str, bool] = {}
        self._profiler = profiler or ControlFoleyProfiler()
        self._cache = cache
        self._condition_cache = condition_cache

    @property
    def descriptor(self) -> ModelDescriptor:
        return self._descriptor

    def load(self, session: ModelSession) -> None:
        if session.deployment.descriptor != self.descriptor:
            raise InvocationRejectedError("ControlFoley deployment selects another adapter")
        configuration = session.deployment.configuration
        l2_cache_enabled = set(configuration) == _L2_CACHED_STAGED_CONFIGURATION_KEYS
        cache_enabled = set(configuration) in (
            _CACHED_STAGED_CONFIGURATION_KEYS,
            _L2_CACHED_STAGED_CONFIGURATION_KEYS,
        )
        if set(configuration) == _CONFIGURATION_KEYS:
            validated = dict(self._runner.validate(configuration))
            deployment_sha = configuration.get("deployment_manifest_sha256")
            if (
                not isinstance(deployment_sha, str)
                or session.deployment.fingerprint != deployment_sha
            ):
                raise InvocationRejectedError(
                    "Core deployment fingerprint must equal the sealed manifest SHA-256"
                )
            _validate_validation_report(validated, configuration)
            operation = UPSTREAM_PARITY_OPERATION
        else:
            _validate_staged_deployment(session)
            if self._staged_path is None:
                raise InvocationRejectedError(
                    "experimental staged deployment requires an operator-supplied backend"
                )
            if cache_enabled:
                if self._cache is None:
                    raise InvocationRejectedError(
                        "cached staged deployment requires an explicit adapter cache"
                    )
                try:
                    self._cache.policy.assert_configuration(configuration)
                except ValueError as error:
                    raise InvocationRejectedError(
                        "cached staged deployment does not match the cache policy"
                    ) from error
                if not self._staged_path.supports_preprocess_cache:
                    raise InvocationRejectedError(
                        "cached staged deployment requires an explicit L1 safe-bytes codec"
                    )
            if l2_cache_enabled:
                if self._condition_cache is None:
                    raise InvocationRejectedError(
                        "L2 deployment requires an explicit adapter condition cache"
                    )
                try:
                    self._condition_cache.base_policy.assert_configuration(configuration)
                    self._condition_cache.policy.assert_configuration(configuration)
                    self._condition_cache.policy.assert_operator_separation(
                        Path(configuration["source_dir"]),
                        Path(configuration["weights_dir"]),
                    )
                except (TypeError, ValueError) as error:
                    raise InvocationRejectedError(
                        "L2 deployment does not match the adapter condition cache policies"
                    ) from error
                if not self._staged_path.supports_condition_feature_cache:
                    raise InvocationRejectedError(
                        "L2 deployment requires an explicit condition feature codec/split"
                    )
                try:
                    condition_identity = self._staged_path.condition_cache_identity
                except Exception as error:
                    raise InvocationRejectedError(
                        "L2 backend condition cache identity is unavailable"
                    ) from error
                expected_identity = (
                    configuration["l2_cache_codec_version"],
                    configuration["l2_cache_schema_version"],
                )
                if condition_identity != expected_identity:
                    raise InvocationRejectedError(
                        "L2 backend condition cache identity does not match the deployment"
                    )
            validated = dict(
                self._staged_path.load(
                    session.session_id, configuration, session.deployment.fingerprint
                )
            )
            operation = EXPERIMENTAL_STAGED_OPERATION
        self._sessions[session.session_id] = validated
        self._session_operations[session.session_id] = operation
        self._session_cache_enabled[session.session_id] = cache_enabled
        self._session_l2_cache_enabled[session.session_id] = l2_cache_enabled

    def invoke(
        self,
        session: ModelSession,
        invocation: ModelInvocation,
        context: ExecutionContext,
    ) -> InvocationResult:
        context.cancellation_token.raise_if_cancelled()
        if session.session_id not in self._sessions:
            raise InvocationRejectedError("ControlFoley session is not loaded")
        selected_operation = self._session_operations[session.session_id]
        if invocation.operation != selected_operation:
            raise InvocationRejectedError(
                "invocation operation does not match the sealed ControlFoley deployment"
            )
        try:
            request = ControlFoleyLocalRequest.from_invocation(invocation)
        except (TypeError, ValueError) as error:
            raise InvocationRejectedError("invalid ControlFoley local request") from error
        invocation_fingerprint: Optional[str] = None
        profile_binding: Optional[ControlFoleyProfileBinding] = None
        if self._profiler.level is not ControlFoleyProfileLevel.OFF:
            try:
                invocation_fingerprint = controlfoley_invocation_fingerprint(request)
                configuration = session.deployment.configuration
                profile_binding = ControlFoleyProfileBinding(
                    operation=selected_operation,
                    deployment_fingerprint=session.deployment.fingerprint,
                    deployment_manifest_sha256=configuration["deployment_manifest_sha256"],
                    source_revision=configuration["source_revision"],
                    checkpoint_sha256=configuration["checkpoint_sha256"],
                    canonical_invocation_sha256=invocation_fingerprint,
                    staged_backend_id=(
                        configuration["staged_backend_id"]
                        if selected_operation == EXPERIMENTAL_STAGED_OPERATION
                        else None
                    ),
                )
            except (KeyError, OSError, TypeError, ValueError) as error:
                raise InvocationRejectedError(
                    "profile execution binding could not be sealed"
                ) from error
        recorder = self._profiler.begin(selected_operation, profile_binding)
        try:
            if selected_operation == EXPERIMENTAL_STAGED_OPERATION:
                return self._invoke_staged_profiled(
                    session,
                    invocation,
                    request,
                    context,
                    recorder,
                    invocation_fingerprint,
                )
            with recorder.observe_upstream():
                content, provenance = self._runner.invoke(
                    session.deployment.configuration, request, context
                )
            context.cancellation_token.raise_if_cancelled()
            self._assert_profile_invocation_unchanged(request, invocation_fingerprint)
            if not isinstance(content, bytes) or not content:
                raise InvocationRejectedError("upstream runner returned no audio bytes")
            self._validate_artifact_provenance(content, provenance, session)
            return InvocationResult(
                invocation_id=invocation.invocation_id,
                artifacts=(
                    ProducedArtifact(
                        name="controlfoley.flac",
                        media_type="audio/flac",
                        content=content,
                        metadata=dict(provenance),
                    ),
                ),
                metadata={
                    "operation": UPSTREAM_PARITY_OPERATION,
                    "deployment": self._sessions[session.session_id],
                    "manifest": _adapter_result_manifest(request, content, provenance),
                },
                profile=recorder.finish(),
            )
        except BaseException:
            recorder.abandon()
            raise

    def _invoke_staged_profiled(
        self,
        session: ModelSession,
        invocation: ModelInvocation,
        request: ControlFoleyLocalRequest,
        context: ExecutionContext,
        recorder: ControlFoleyProfileRecorder,
        profile_invocation_fingerprint: Optional[str],
    ) -> InvocationResult:
        if self._staged_path is None:
            raise InvocationRejectedError("experimental staged backend is unavailable")
        staged_path = self._staged_path
        condition_execution_observable = staged_path.supports_condition_feature_cache
        cache_transaction: Optional[ControlFoleyCacheTransaction] = None
        condition_transaction: Optional[ControlFoleyConditionCacheTransaction] = None
        cached_preprocess_payload: Optional[bytes] = None
        cached_condition_payloads: Optional[Mapping[str, bytes]] = None
        condition_encoder_calls = 0
        condition_projection_calls = 0
        if self._session_cache_enabled.get(session.session_id, False):
            if self._cache is None:
                raise InvocationRejectedError("cached staged session has no cache implementation")
            cache_transaction = self._cache.begin(
                request, session.deployment.configuration, session.deployment.fingerprint
            )
            cached_preprocess_payload = cache_transaction.cached_preprocess
        if self._session_l2_cache_enabled.get(session.session_id, False):
            if self._condition_cache is None:
                raise InvocationRejectedError(
                    "L2 cached staged session has no condition cache implementation"
                )
            condition_transaction = self._condition_cache.begin(
                request, session.deployment.configuration, session.deployment.fingerprint
            )
            cached_condition_payloads = condition_transaction.cached_payloads

        def decode_cached_preprocess(payload: bytes) -> ControlFoleyStageValue:
            return staged_path.decode_cached_preprocess(
                session.session_id, payload, request, context
            )

        def reject_cached_preprocess() -> None:
            if cache_transaction is not None:
                cache_transaction.reject_cached_preprocess()

        def capture_preprocess(value: ControlFoleyStageValue) -> None:
            if cache_transaction is None:
                return
            try:
                payload = staged_path.encode_preprocess(session.session_id, value, request)
            except Exception:
                cache_transaction.record_fault("codec_fault")
                return
            cache_transaction.stage_preprocess(payload)

        def reject_condition_feature(role: str) -> None:
            if condition_transaction is not None:
                condition_transaction.reject(role)

        def capture_condition_feature(feature: ControlFoleyConditionFeature) -> None:
            if condition_transaction is None:
                return
            try:
                payload = staged_path.encode_condition_feature(session.session_id, feature, request)
            except Exception:
                condition_transaction.record_fault("codec_fault")
                return
            condition_transaction.stage(feature.role, payload)

        def record_condition_encoder(role: str) -> None:
            nonlocal condition_encoder_calls
            del role
            condition_encoder_calls += 1

        def record_condition_projection() -> None:
            nonlocal condition_projection_calls
            condition_projection_calls += 1

        try:
            try:
                invocation_fingerprint = controlfoley_invocation_fingerprint(request)
            except (OSError, TypeError, ValueError) as error:
                raise InvocationRejectedError(
                    "experimental staged inputs could not be fingerprinted"
                ) from error
            artifact, stages = staged_path.invoke(
                session.session_id,
                request,
                context,
                recorder,
                cached_preprocess_payload=cached_preprocess_payload,
                decode_cached_preprocess=(
                    decode_cached_preprocess if cached_preprocess_payload is not None else None
                ),
                reject_cached_preprocess=(
                    reject_cached_preprocess if cached_preprocess_payload is not None else None
                ),
                capture_preprocess=(capture_preprocess if cache_transaction is not None else None),
                condition_cache_payloads=cached_condition_payloads,
                reject_condition_feature=(
                    reject_condition_feature if condition_transaction is not None else None
                ),
                capture_condition_feature=(
                    capture_condition_feature if condition_transaction is not None else None
                ),
                on_condition_encoder=record_condition_encoder,
                on_condition_projection=record_condition_projection,
            )
            self._assert_profile_invocation_unchanged(request, profile_invocation_fingerprint)
            content = artifact.content
            digest = hashlib.sha256(content).hexdigest()
            comparison = ControlFoleyComparisonEvidence.unmeasured(
                deployment_manifest_sha256=session.deployment.configuration[
                    "deployment_manifest_sha256"
                ],
                candidate_deployment_fingerprint=session.deployment.fingerprint,
                candidate_backend_id=session.deployment.configuration["staged_backend_id"],
                source_revision=session.deployment.configuration["source_revision"],
                checkpoint_sha256=session.deployment.configuration["checkpoint_sha256"],
                canonical_invocation_sha256=invocation_fingerprint,
                candidate_sha256=digest,
            )
            artifact_format = {"audio/flac": "flac", "audio/wav": "wav"}[artifact.media_type]
            provenance = {
                "schema_version": 1,
                "kind": "controlfoley-experimental-staged-local-artifact",
                "execution_path": EXPERIMENTAL_STAGED_OPERATION,
                "backend_id": session.deployment.configuration["staged_backend_id"],
                "deployment_fingerprint": session.deployment.fingerprint,
                "deployment_manifest_sha256": session.deployment.configuration[
                    "deployment_manifest_sha256"
                ],
                "source_revision": session.deployment.configuration["source_revision"],
                "checkpoint_sha256": session.deployment.configuration["checkpoint_sha256"],
                "sha256": digest,
                "size_bytes": len(content),
                "format": artifact_format,
                "stage_order": tuple(value.stage.value for value in stages),
            }
            context.cancellation_token.raise_if_cancelled()
            cache_report = CacheReport()
            condition_cache_report = CacheReport()
            if condition_transaction is not None and self._condition_cache is not None:
                try:
                    condition_cache_report = self._condition_cache.commit(
                        condition_transaction,
                        request,
                        session.deployment.configuration,
                        session.deployment.fingerprint,
                        context.cancellation_token.raise_if_cancelled,
                    )
                except ControlFoleyCacheInputDrift as error:
                    raise InvocationRejectedError(
                        "L2 cached invocation inputs changed during execution"
                    ) from error
            if cache_transaction is not None and self._cache is not None:
                try:
                    cache_report = self._cache.commit(
                        cache_transaction,
                        request,
                        session.deployment.configuration,
                        session.deployment.fingerprint,
                    )
                except ControlFoleyCacheInputDrift as error:
                    if condition_transaction is not None:
                        condition_transaction.rollback_new_writes()
                    raise InvocationRejectedError(
                        "cached invocation inputs changed during execution"
                    ) from error
            context.cancellation_token.raise_if_cancelled()
            if condition_transaction is not None and self._condition_cache is not None:
                try:
                    self._condition_cache.assert_current(
                        condition_transaction,
                        request,
                        session.deployment.configuration,
                        session.deployment.fingerprint,
                    )
                except ControlFoleyCacheInputDrift as error:
                    raise InvocationRejectedError(
                        "L2 cached invocation inputs changed after persistence"
                    ) from error
            if condition_transaction is not None:
                cache_report = merge_controlfoley_cache_reports(
                    cache_report,
                    condition_cache_report,
                    encoder_calls=condition_encoder_calls,
                    projection_calls=condition_projection_calls,
                )
            return InvocationResult(
                invocation_id=invocation.invocation_id,
                artifacts=(
                    ProducedArtifact(
                        name=artifact.name,
                        media_type=artifact.media_type,
                        content=content,
                        metadata=provenance,
                    ),
                ),
                metadata={
                    "operation": EXPERIMENTAL_STAGED_OPERATION,
                    "experimental": True,
                    "default_operation": UPSTREAM_PARITY_OPERATION,
                    "deployment": self._sessions[session.session_id],
                    "stage_order": tuple(stage.value for stage in CONTROLFOLEY_STAGE_ORDER),
                    "condition_execution": {
                        "observable": condition_execution_observable,
                        "encoder_calls": (
                            condition_encoder_calls if condition_execution_observable else None
                        ),
                        "projection_calls": (
                            condition_projection_calls if condition_execution_observable else None
                        ),
                    },
                    "comparison": comparison.to_dict(),
                },
                warnings=(
                    "experimental staged path; upstream parity and performance are unmeasured",
                ),
                profile=recorder.finish(),
                cache=cache_report,
            )
        except BaseException:
            if cache_transaction is not None:
                cache_transaction.abort()
            if condition_transaction is not None:
                condition_transaction.rollback_new_writes()
                condition_transaction.abort()
            raise

    def _assert_profile_invocation_unchanged(
        self,
        request: ControlFoleyLocalRequest,
        expected_fingerprint: Optional[str],
    ) -> None:
        if expected_fingerprint is None:
            return
        try:
            current = controlfoley_invocation_fingerprint(request)
        except (OSError, TypeError, ValueError) as error:
            raise InvocationRejectedError(
                "profile execution inputs could not be revalidated"
            ) from error
        if current != expected_fingerprint:
            raise InvocationRejectedError("profile execution inputs changed during the invocation")

    def unload(self, session: ModelSession) -> None:
        operation = self._session_operations.pop(session.session_id, None)
        cache_enabled = self._session_cache_enabled.pop(session.session_id, False)
        l2_cache_enabled = self._session_l2_cache_enabled.pop(session.session_id, False)
        try:
            if operation == EXPERIMENTAL_STAGED_OPERATION and self._staged_path is not None:
                self._staged_path.unload(session.session_id)
        finally:
            self._sessions.pop(session.session_id, None)
            if cache_enabled and self._cache is not None:
                self._cache.invalidate_deployment(session.deployment.fingerprint)
            if l2_cache_enabled and self._condition_cache is not None:
                # Valid immutable L2 entries survive session/process restart;
                # unload only runs bounded orphan/expiry cleanup.
                self._condition_cache.release_deployment(session.deployment.fingerprint)

    def invalidate_cache(self, deployment_fingerprint: Optional[str] = None) -> int:
        """Explicitly invalidate experimental cache state without changing execution."""

        if self._cache is None:
            base_count = 0
        elif deployment_fingerprint is None:
            base_count = self._cache.invalidate_all()
        else:
            base_count = self._cache.invalidate_deployment(deployment_fingerprint)
        if self._condition_cache is None:
            return base_count
        if deployment_fingerprint is None:
            return base_count + self._condition_cache.invalidate_all()
        return base_count + self._condition_cache.invalidate_deployment(deployment_fingerprint)

    def close_cache(self) -> None:
        if self._cache is not None:
            self._cache.close()
        if self._condition_cache is not None:
            self._condition_cache.close()

    def _validate_artifact_provenance(
        self, content: bytes, provenance: Mapping[str, Any], session: ModelSession
    ) -> None:
        expected = {
            "sha256": hashlib.sha256(content).hexdigest(),
            "source_revision": session.deployment.configuration["source_revision"],
            "deployment_manifest_sha256": session.deployment.configuration[
                "deployment_manifest_sha256"
            ],
            "variant": session.deployment.configuration["variant"],
            "precision": session.deployment.configuration["precision"],
            "checkpoint_sha256": session.deployment.configuration["checkpoint_sha256"],
            "format": "flac",
        }
        if set(provenance) != _PROVENANCE_KEYS or any(
            provenance.get(name) != value for name, value in expected.items()
        ):
            raise InvocationRejectedError(
                "upstream runner artifact provenance is incomplete or mismatched"
            )
        if provenance["size_bytes"] != len(content):
            raise InvocationRejectedError("upstream runner artifact size does not match content")
        _load_worker_report_value(provenance["worker"])
        if _contains_path(provenance):
            raise InvocationRejectedError("upstream runner provenance must be path-free")


def _adapter_result_manifest(
    request: ControlFoleyLocalRequest, content: bytes, provenance: Mapping[str, Any]
) -> Mapping[str, Any]:
    """Return local, path-free execution evidence; it is not a parity claim."""

    return {
        "schema_version": 1,
        "kind": "controlfoley-upstream-parity-local-result",
        "source_revision": provenance["source_revision"],
        "deployment_manifest_sha256": provenance["deployment_manifest_sha256"],
        "variant": provenance["variant"],
        "precision": provenance["precision"],
        "checkpoint_sha256": provenance["checkpoint_sha256"],
        "parameters": {
            "task": request.task.value,
            "duration_seconds": request.duration_seconds,
            "cfg_strength": request.guidance_scale,
            "num_steps": request.num_steps,
            "seed": request.seed,
            "negative_prompt": None,
            "skip_video_composite": False,
            "mask_away_clip": False,
        },
        "inputs": {
            "video": None
            if request.video_path is None
            else fingerprint_file(request.video_path).to_dict(),
            "reference_audio": (
                None
                if request.reference_audio_path is None
                else fingerprint_file(request.reference_audio_path).to_dict()
            ),
            "prompt": None if request.prompt is None else _fingerprint_text(request.prompt),
        },
        "artifact": {
            "name": "controlfoley.flac",
            "media_type": "audio/flac",
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
        },
        "comparison": "unmeasured; RTX 4090 parity evidence remains deferred",
    }


def _fingerprint_text(value: str) -> Mapping[str, Any]:
    encoded = value.encode("utf-8")
    return {
        "status": "verified",
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "size_bytes": len(encoded),
    }


def _load_worker_report_value(value: Any) -> None:
    if not isinstance(value, Mapping):
        raise InvocationRejectedError("upstream worker provenance is invalid")
    # Reuse the on-disk validator through a small shape-equivalent check.
    if set(value) != {"wall_time_seconds", "peak_allocated_bytes", "peak_reserved_bytes"}:
        raise InvocationRejectedError("upstream worker provenance has missing or unexpected fields")
    wall = value["wall_time_seconds"]
    if (
        isinstance(wall, bool)
        or not isinstance(wall, (int, float))
        or not math.isfinite(wall)
        or wall <= 0
    ):
        raise InvocationRejectedError("upstream worker provenance has invalid wall time")
    if any(
        isinstance(value[name], bool) or not isinstance(value[name], int) or value[name] < 0
        for name in ("peak_allocated_bytes", "peak_reserved_bytes")
    ):
        raise InvocationRejectedError("upstream worker provenance has invalid peaks")


def _contains_path(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(_contains_path(item) for item in value.values())
    if isinstance(value, (tuple, list)):
        return any(_contains_path(item) for item in value)
    return isinstance(value, str) and ("/" in value or "\\" in value)
