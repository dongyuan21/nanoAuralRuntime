#!/usr/bin/env python3
"""ControlFoley Phase 2A locked-upstream baseline harness.

Without ``--execute-upstream`` this command only validates manifests and
prerequisites. Execution is opt-in, uses an argv-only child process to run the
pinned source's original ``demo.py`` twice, and refuses pending fingerprints.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from nano_aural_runtime_controlfoley.baseline import (
    BaselineResultManifest,
    ControlFoleyDeploymentManifest,
    Fingerprint,
    FixtureManifest,
    SchemaValidationError,
    collect_sanitized_environment,
    compare_waveforms,
    detect_gpu_prerequisites,
    fingerprint_file,
    fingerprint_text,
    load_json,
    manifest_sha256,
    verify_result_bindings,
    write_json,
)

_METRICS = (
    "peak",
    "rms",
    "mae",
    "max_absolute_error",
    "waveform_cosine_similarity",
    "mel_spectrogram_distance",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument(
        "--source-dir", type=Path, default=os.environ.get("CONTROLFOLEY_SOURCE_DIR")
    )
    parser.add_argument(
        "--weights-dir", type=Path, default=os.environ.get("CONTROLFOLEY_WEIGHTS_DIR")
    )
    parser.add_argument("--write-result-template", type=Path)
    parser.add_argument(
        "--write-verified-deployment",
        type=Path,
        help="write a separate manifest with real fingerprints calculated from --weights-dir",
    )
    parser.add_argument(
        "--write-verified-fixture",
        type=Path,
        help="write a separate fixture with real fingerprints calculated from its declared inputs",
    )
    parser.add_argument("--execute-upstream", action="store_true")
    parser.add_argument("--video", type=Path)
    parser.add_argument("--audio", type=Path)
    parser.add_argument("--prompt")
    parser.add_argument("--negative-prompt", default="")
    parser.add_argument("--duration", type=float)
    parser.add_argument("--cfg-strength", type=float)
    parser.add_argument("--num-steps", type=int)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--skip-video-composite", action="store_true")
    parser.add_argument("--mask-away-clip", action="store_true")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--repeat-output-dir", type=Path)
    parser.add_argument("--result", type=Path)
    return parser


def _empty_metrics() -> Dict[str, None]:
    return {metric: None for metric in _METRICS}


def _planned_result(
    deployment: ControlFoleyDeploymentManifest, fixture: FixtureManifest
) -> BaselineResultManifest:
    result = BaselineResultManifest.from_dict(
        {
            "schema_version": 1,
            "run_id": "pending-{0}".format(fixture.fixture_id),
            "fixture_id": fixture.fixture_id,
            "deployment_id": deployment.deployment_id,
            "deployment_manifest_sha256": manifest_sha256(deployment.to_dict()),
            "fixture_manifest_sha256": manifest_sha256(fixture.to_dict()),
            "source_revision": deployment.source_revision,
            "checkpoint": deployment.checkpoint.to_dict(),
            "input_fingerprints": {
                item.role: item.fingerprint.to_dict() for item in fixture.inputs
            },
            "parameters": {
                "task": fixture.task,
                "variant": deployment.variant,
                "precision": deployment.precision,
                "duration_seconds": fixture.duration_seconds,
                "cfg_strength": fixture.guidance_scale,
                "num_steps": fixture.num_steps,
                "seed": fixture.seed,
            },
            "state": "planned",
            "environment": collect_sanitized_environment(),
            "waveform": {"channels": None, "samples": None, "sample_rate": None},
            "wall_time_seconds": None,
            "peak_allocated_bytes": None,
            "peak_reserved_bytes": None,
            "self_repeat_metrics": _empty_metrics(),
            "self_repeat_thresholds": _empty_metrics(),
            "repeat_evidence": [],
        }
    )
    return result


def _input_values(
    fixture: FixtureManifest, arguments: argparse.Namespace
) -> Tuple[Dict[str, Fingerprint], Sequence[str]]:
    actual: Dict[str, Fingerprint] = {}
    reasons = []
    declared_roles = {item.role for item in fixture.inputs}
    if arguments.video is not None and "video" not in declared_roles:
        reasons.append("--video is not declared by this fixture")
    if arguments.audio is not None and "reference_audio" not in declared_roles:
        reasons.append("--audio is not declared by this fixture")
    if arguments.prompt is not None and "prompt" not in declared_roles:
        reasons.append("--prompt is not declared by this fixture")
    for item in fixture.inputs:
        if item.role == "video":
            value = arguments.video
            if value is None or not value.is_file():
                reasons.append("--video must name a readable file")
                continue
            actual[item.role] = fingerprint_file(value)
        elif item.role == "reference_audio":
            value = arguments.audio
            if value is None or not value.is_file():
                reasons.append("--audio must name a readable file")
                continue
            actual[item.role] = fingerprint_file(value)
        elif item.role == "prompt":
            if arguments.prompt is None or not arguments.prompt.strip():
                reasons.append("--prompt must be supplied for this fixture")
                continue
            actual[item.role] = fingerprint_text(arguments.prompt)
        else:
            reasons.append("unsupported fixture input role: {0}".format(item.role))
            continue
        if item.fingerprint.status != "verified":
            reasons.append("fixture fingerprint is pending for {0}".format(item.role))
        elif item.fingerprint != actual[item.role]:
            reasons.append("fixture fingerprint does not match {0}".format(item.role))
    return actual, reasons


def _sealed_deployment(
    deployment: ControlFoleyDeploymentManifest, weights_dir: Optional[Path]
) -> ControlFoleyDeploymentManifest:
    if weights_dir is None or not weights_dir.is_dir():
        raise SchemaValidationError(
            "--write-verified-deployment requires an existing --weights-dir"
        )
    data = deployment.to_dict()
    checkpoint_path = weights_dir / deployment.checkpoint_relative_path
    if not checkpoint_path.is_file():
        raise SchemaValidationError("required checkpoint is missing")
    data["checkpoint"] = fingerprint_file(checkpoint_path).to_dict()
    for item in data["external_weights"]:
        candidate = weights_dir / item["relative_path"]
        if not candidate.is_file():
            raise SchemaValidationError(
                "required external weight is missing: {0}".format(item["relative_path"])
            )
        item["fingerprint"] = fingerprint_file(candidate).to_dict()
    return ControlFoleyDeploymentManifest.from_dict(data)


def _sealed_fixture(fixture: FixtureManifest, arguments: argparse.Namespace) -> FixtureManifest:
    actual, reasons = _input_values(fixture, arguments)
    non_pending = [reason for reason in reasons if "fingerprint is pending" not in reason]
    if non_pending:
        raise SchemaValidationError(
            "cannot fingerprint fixture: {0}".format("; ".join(non_pending))
        )
    if len(actual) != len(fixture.inputs):
        raise SchemaValidationError("cannot fingerprint every declared fixture input")
    data = fixture.to_dict()
    for item in data["inputs"]:
        item["fingerprint"] = actual[item["role"]].to_dict()
    return FixtureManifest.from_dict(data)


def _demo_arguments(
    deployment: ControlFoleyDeploymentManifest,
    fixture: FixtureManifest,
    arguments: argparse.Namespace,
    output_dir: Path,
) -> Tuple[Sequence[str], Mapping[str, Any]]:
    duration = fixture.duration_seconds if arguments.duration is None else arguments.duration
    cfg_strength = (
        fixture.guidance_scale if arguments.cfg_strength is None else arguments.cfg_strength
    )
    num_steps = fixture.num_steps if arguments.num_steps is None else arguments.num_steps
    seed = fixture.seed if arguments.seed is None else arguments.seed
    if duration <= 0 or cfg_strength < 0 or num_steps < 1:
        raise SchemaValidationError("duration/cfg-strength/num-steps override is invalid")
    demo_argv = [
        "--variant",
        deployment.variant,
        "--duration",
        str(duration),
        "--cfg_strength",
        str(cfg_strength),
        "--num_steps",
        str(num_steps),
        "--output",
        str(output_dir.resolve()),
        "--seed",
        str(seed),
    ]
    if arguments.video is not None:
        demo_argv.extend(("--video", str(arguments.video.resolve())))
    if arguments.audio is not None:
        demo_argv.extend(("--audio", str(arguments.audio.resolve())))
    if arguments.prompt is not None:
        demo_argv.extend(("--prompt", arguments.prompt))
    if arguments.negative_prompt:
        demo_argv.extend(("--negative_prompt", arguments.negative_prompt))
    if arguments.skip_video_composite:
        demo_argv.append("--skip_video_composite")
    if arguments.mask_away_clip:
        demo_argv.append("--mask_away_clip")
    sanitized = {
        "task": fixture.task,
        "variant": deployment.variant,
        "precision": deployment.precision,
        "video": None if arguments.video is None else fingerprint_file(arguments.video).to_dict(),
        "audio": None if arguments.audio is None else fingerprint_file(arguments.audio).to_dict(),
        "duration_seconds": duration,
        "cfg_strength": cfg_strength,
        "num_steps": num_steps,
        "seed": seed,
        "skip_video_composite": arguments.skip_video_composite,
        "mask_away_clip": arguments.mask_away_clip,
        "output": {"path_redacted": True, "repeat_specific": True},
        "prompt": None
        if arguments.prompt is None
        else fingerprint_text(arguments.prompt).to_dict(),
        "negative_prompt": (
            None
            if not arguments.negative_prompt
            else fingerprint_text(arguments.negative_prompt).to_dict()
        ),
    }
    return demo_argv, sanitized


def _run_locked_demo(
    source_dir: Path, weights_dir: Path, demo_argv: Sequence[str], output_dir: Path
) -> Mapping[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=False)
    report_path = output_dir / ".baseline-worker.json"
    command = [
        sys.executable,
        "-m",
        "nano_aural_runtime_controlfoley.upstream_worker",
        "--source-dir",
        str(source_dir.resolve()),
        "--weights-dir",
        str(weights_dir.resolve()),
        "--report",
        str(report_path),
        "--",
        *demo_argv,
    ]
    subprocess.run(command, check=True, cwd=source_dir.resolve(), shell=False)
    with report_path.open(encoding="utf-8") as handle:
        report = json.load(handle)
    for name in ("wall_time_seconds", "peak_allocated_bytes", "peak_reserved_bytes"):
        if name not in report:
            raise SchemaValidationError("upstream worker report is missing {0}".format(name))
    return report


def _find_audio_output(output_dir: Path) -> Path:
    audio = sorted(
        path
        for suffix in ("*.flac", "*.wav")
        for path in output_dir.rglob(suffix)
        if path.is_file()
    )
    if len(audio) != 1:
        raise SchemaValidationError(
            "expected exactly one generated FLAC/WAV file, found {0}".format(len(audio))
        )
    return audio[0]


def _repeat_evidence(
    output: Path, report: Mapping[str, Any], waveform: Mapping[str, int]
) -> Dict[str, Any]:
    return {
        "output": fingerprint_file(output).to_dict(),
        "waveform": dict(waveform),
        "wall_time_seconds": report["wall_time_seconds"],
        "peak_allocated_bytes": report["peak_allocated_bytes"],
        "peak_reserved_bytes": report["peak_reserved_bytes"],
    }


def _execute(
    deployment: ControlFoleyDeploymentManifest,
    fixture: FixtureManifest,
    arguments: argparse.Namespace,
) -> BaselineResultManifest:
    prerequisites = detect_gpu_prerequisites(
        arguments.source_dir, arguments.weights_dir, deployment
    )
    actual_inputs, input_reasons = _input_values(fixture, arguments)
    override_reasons = []
    if any(
        value is not None
        for value in (
            arguments.duration,
            arguments.cfg_strength,
            arguments.num_steps,
            arguments.seed,
        )
    ):
        override_reasons.append("duration/cfg-strength/num-steps/seed overrides are forbidden")
    if arguments.negative_prompt != "":
        override_reasons.append("negative-prompt is locked to the upstream default empty string")
    if arguments.skip_video_composite:
        override_reasons.append("skip-video-composite is locked to false")
    if arguments.mask_away_clip:
        override_reasons.append("mask-away-clip is locked to false")
    required = (arguments.output_dir, arguments.repeat_output_dir, arguments.result)
    missing_outputs = (
        ["execution requires --output-dir, --repeat-output-dir, and --result"]
        if any(value is None for value in required)
        else []
    )
    reasons = [*prerequisites.reasons, *input_reasons, *override_reasons, *missing_outputs]
    if reasons:
        raise SchemaValidationError("upstream execution refused: {0}".format("; ".join(reasons)))
    assert arguments.source_dir is not None
    assert arguments.weights_dir is not None
    assert arguments.output_dir is not None
    assert arguments.repeat_output_dir is not None
    first_argv, sanitized = _demo_arguments(deployment, fixture, arguments, arguments.output_dir)
    second_argv, _ = _demo_arguments(deployment, fixture, arguments, arguments.repeat_output_dir)
    first_report = _run_locked_demo(
        arguments.source_dir, arguments.weights_dir, first_argv, arguments.output_dir
    )
    second_report = _run_locked_demo(
        arguments.source_dir, arguments.weights_dir, second_argv, arguments.repeat_output_dir
    )
    first_output = _find_audio_output(arguments.output_dir)
    second_output = _find_audio_output(arguments.repeat_output_dir)
    metrics, waveform = compare_waveforms(first_output, second_output)
    first_evidence = _repeat_evidence(first_output, first_report, waveform)
    second_evidence = _repeat_evidence(second_output, second_report, waveform)
    result = BaselineResultManifest.from_dict(
        {
            "schema_version": 1,
            "run_id": "{0}-repeat".format(fixture.fixture_id),
            "fixture_id": fixture.fixture_id,
            "deployment_id": deployment.deployment_id,
            "deployment_manifest_sha256": manifest_sha256(deployment.to_dict()),
            "fixture_manifest_sha256": manifest_sha256(fixture.to_dict()),
            "source_revision": deployment.source_revision,
            "checkpoint": deployment.checkpoint.to_dict(),
            "input_fingerprints": {role: value.to_dict() for role, value in actual_inputs.items()},
            "parameters": sanitized,
            "state": "completed",
            "environment": collect_sanitized_environment(),
            "waveform": waveform,
            "wall_time_seconds": first_report["wall_time_seconds"],
            "peak_allocated_bytes": first_report["peak_allocated_bytes"],
            "peak_reserved_bytes": first_report["peak_reserved_bytes"],
            "self_repeat_metrics": metrics,
            "self_repeat_thresholds": _empty_metrics(),
            "repeat_evidence": [first_evidence, second_evidence],
        }
    )
    verify_result_bindings(deployment, fixture, result)
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = _parser().parse_args(argv)
    deployment = ControlFoleyDeploymentManifest.from_dict(load_json(arguments.deployment))
    fixture = FixtureManifest.from_dict(load_json(arguments.fixture))
    if arguments.write_verified_deployment is not None:
        deployment = _sealed_deployment(deployment, arguments.weights_dir)
        write_json(arguments.write_verified_deployment, deployment.to_dict())
    if arguments.write_verified_fixture is not None:
        fixture = _sealed_fixture(fixture, arguments)
        write_json(arguments.write_verified_fixture, fixture.to_dict())
    prerequisites = detect_gpu_prerequisites(
        arguments.source_dir, arguments.weights_dir, deployment
    )
    if arguments.write_result_template is not None:
        write_json(arguments.write_result_template, _planned_result(deployment, fixture).to_dict())
    if arguments.execute_upstream:
        result = _execute(deployment, fixture, arguments)
        assert arguments.result is not None
        write_json(arguments.result, result.to_dict())
    print(
        json.dumps(
            {
                "deployment_id": deployment.deployment_id,
                "fixture_id": fixture.fixture_id,
                "source_revision": deployment.source_revision,
                "gpu_prerequisites": {
                    "available": prerequisites.available,
                    "reasons": list(prerequisites.reasons),
                    "diagnostics": list(prerequisites.diagnostics),
                },
                "execute_upstream": arguments.execute_upstream,
            },
            sort_keys=True,
        )
    )
    return 0 if prerequisites.available or not arguments.execute_upstream else 2


if __name__ == "__main__":
    raise SystemExit(main())
