#!/usr/bin/env python3
"""Temporary repository-local engineering agent for the P12-P14 integration branch.

The file deletes itself before the final branch push. It uses GitHub Models through
this workflow's short-lived GITHUB_TOKEN, exposes a constrained repository toolset,
and refuses to finish a phase until mandatory software gates pass.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[2]
TOKEN = os.environ["GITHUB_TOKEN"]
ENDPOINT = "https://models.github.ai/inference/chat/completions"
MODEL_CANDIDATES = tuple(
    item.strip()
    for item in os.environ.get(
        "NANO_AURAL_ENGINEERING_MODELS",
        "openai/gpt-4.1,openai/gpt-4.1-mini",
    ).split(",")
    if item.strip()
)
MAX_TOOL_OUTPUT = 24000
AUTOMATION_PATHS = frozenset(
    {
        ".github/automation/p12_p14_engineer.py",
        ".github/workflows/implement-p12-p14.yml",
    }
)
TEXT_SUFFIXES = frozenset(
    {
        ".py",
        ".sql",
        ".md",
        ".toml",
        ".yaml",
        ".yml",
        ".json",
        ".txt",
        ".sh",
        ".dockerignore",
    }
)

SYSTEM = """You are the principal engineer editing nanoAuralRuntime. Work through tools, not prose-only plans. Preserve the repository's model-neutral Runtime Core, durable PostgreSQL authority, lease epoch fencing, publication CAS, namespace isolation, and strict security boundaries. Never weaken tests or hide failures. Do not edit the temporary engineering workflow/agent. Prefer small coherent patches, inspect before changing, run focused tests while iterating, and call finish_phase only when production code, migrations, compatibility, documentation, and tests for the requested phase are complete."""


class AgentFailure(RuntimeError):
    pass


def _run(argv: Sequence[str], timeout: int = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=dict(os.environ),
    )


def _trim(value: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    if len(value) <= limit:
        return value
    half = limit // 2
    return value[:half] + "\n...<truncated>...\n" + value[-half:]


def _safe_path(raw: str, *, must_exist: bool = False) -> Path:
    if not isinstance(raw, str) or not raw or "\\" in raw or "\x00" in raw:
        raise AgentFailure("invalid repository path")
    pure = PurePosixPath(raw)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise AgentFailure("unsafe repository path")
    normalized = pure.as_posix()
    if normalized in AUTOMATION_PATHS or normalized.startswith(".git/"):
        raise AgentFailure("temporary automation and .git are immutable")
    path = ROOT.joinpath(*pure.parts)
    resolved_parent = path.parent.resolve()
    if ROOT.resolve() not in (resolved_parent, *resolved_parent.parents):
        raise AgentFailure("path escapes repository")
    if must_exist and not path.exists():
        raise AgentFailure("path does not exist")
    return path


def _text_file(path: Path) -> bool:
    if path.name in {"Dockerfile", ".dockerignore", ".gitignore"}:
        return True
    return path.suffix in TEXT_SUFFIXES


def list_tree(path: str = ".", depth: int = 4) -> str:
    base = ROOT if path == "." else _safe_path(path, must_exist=True)
    if not base.is_dir():
        raise AgentFailure("tree path is not a directory")
    if isinstance(depth, bool) or not isinstance(depth, int) or not 1 <= depth <= 8:
        raise AgentFailure("depth must be an integer from 1 to 8")
    base_depth = len(base.relative_to(ROOT).parts)
    rows: List[str] = []
    for candidate in sorted(base.rglob("*")):
        relative = candidate.relative_to(ROOT)
        if any(part in {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache"} for part in relative.parts):
            continue
        if len(relative.parts) - base_depth > depth:
            continue
        marker = "/" if candidate.is_dir() else ""
        rows.append(relative.as_posix() + marker)
        if len(rows) >= 1200:
            rows.append("...<tree truncated>...")
            break
    return "\n".join(rows)


def read_file(path: str, start_line: int = 1, end_line: int = 400) -> str:
    target = _safe_path(path, must_exist=True)
    if not target.is_file() or not _text_file(target):
        raise AgentFailure("read_file accepts repository text files only")
    if any(isinstance(item, bool) or not isinstance(item, int) for item in (start_line, end_line)):
        raise AgentFailure("line bounds must be integers")
    if start_line < 1 or end_line < start_line or end_line - start_line > 900:
        raise AgentFailure("invalid or excessive line range")
    lines = target.read_text(encoding="utf-8").splitlines()
    selected = lines[start_line - 1 : end_line]
    return "\n".join(f"{index}: {line}" for index, line in enumerate(selected, start=start_line))


def search_code(query: str, path: str = ".", regex: bool = False) -> str:
    if not isinstance(query, str) or not query or len(query) > 500:
        raise AgentFailure("invalid search query")
    base = ROOT if path == "." else _safe_path(path, must_exist=True)
    pattern = re.compile(query) if regex else None
    matches: List[str] = []
    candidates: Iterable[Path]
    if base.is_file():
        candidates = (base,)
    else:
        candidates = sorted(base.rglob("*"))
    for candidate in candidates:
        if not candidate.is_file() or not _text_file(candidate):
            continue
        relative = candidate.relative_to(ROOT)
        if any(part in {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache"} for part in relative.parts):
            continue
        try:
            lines = candidate.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(lines, start=1):
            hit = bool(pattern.search(line)) if pattern is not None else query in line
            if hit:
                matches.append(f"{relative.as_posix()}:{number}:{line}")
                if len(matches) >= 250:
                    matches.append("...<search truncated>...")
                    return "\n".join(matches)
    return "\n".join(matches)


def write_file(path: str, content: str) -> str:
    target = _safe_path(path)
    if not isinstance(content, str):
        raise AgentFailure("file content must be text")
    if len(content.encode("utf-8")) > 900_000:
        raise AgentFailure("single file write is too large")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"wrote {target.relative_to(ROOT).as_posix()} ({len(content.encode('utf-8'))} bytes)"


def delete_file(path: str) -> str:
    target = _safe_path(path, must_exist=True)
    if not target.is_file() or target.is_symlink():
        raise AgentFailure("delete_file accepts regular files only")
    target.unlink()
    return f"deleted {target.relative_to(ROOT).as_posix()}"


def apply_patch(patch: str) -> str:
    if not isinstance(patch, str) or not patch.strip() or len(patch.encode("utf-8")) > 1_500_000:
        raise AgentFailure("invalid patch")
    changed = set()
    for line in patch.splitlines():
        if line.startswith("+++ b/") or line.startswith("--- a/"):
            raw = line[6:]
            if raw != "/dev/null":
                changed.add(PurePosixPath(raw).as_posix())
    if not changed:
        raise AgentFailure("patch contains no repository paths")
    for path in changed:
        _safe_path(path)
    checked = subprocess.run(
        ["git", "apply", "--check", "--whitespace=fix", "-"],
        cwd=ROOT,
        input=patch,
        text=True,
        capture_output=True,
        timeout=60,
    )
    if checked.returncode:
        raise AgentFailure("patch check failed:\n" + _trim(checked.stderr, 6000))
    applied = subprocess.run(
        ["git", "apply", "--whitespace=fix", "-"],
        cwd=ROOT,
        input=patch,
        text=True,
        capture_output=True,
        timeout=60,
    )
    if applied.returncode:
        raise AgentFailure("patch apply failed:\n" + _trim(applied.stderr, 6000))
    return "applied patch to: " + ", ".join(sorted(changed))


def run_command(argv: Sequence[str], timeout: int = 300) -> str:
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) for item in argv):
        raise AgentFailure("argv must be a non-empty string list")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 1200:
        raise AgentFailure("timeout is out of range")
    allowed = False
    if argv[:2] == ["python", "-m"] and len(argv) >= 3 and argv[2] in {
        "pytest",
        "ruff",
        "pyright",
        "compileall",
    }:
        allowed = True
    elif argv[0] == "git" and len(argv) >= 2 and argv[1] in {
        "diff",
        "status",
        "log",
        "show",
        "grep",
    }:
        allowed = True
    if not allowed:
        raise AgentFailure("command is outside the engineering allowlist")
    for argument in argv:
        if "\x00" in argument or argument.startswith("/") or "../" in argument:
            raise AgentFailure("unsafe command argument")
    completed = _run(argv, timeout=timeout)
    return _trim(
        f"exit={completed.returncode}\n--- stdout ---\n{completed.stdout}\n--- stderr ---\n{completed.stderr}"
    )


def git_diff(staged: bool = False) -> str:
    argv = ["git", "diff"]
    if staged:
        argv.append("--cached")
    argv.extend(["--stat", "--patch"])
    return _trim(_run(argv, timeout=60).stdout, 40000)


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_tree",
            "description": "List repository paths below a directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "depth": {"type": "integer", "minimum": 1, "maximum": 8},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a bounded line range from a repository text file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "Search repository text files for a literal or regular expression.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "path": {"type": "string"},
                    "regex": {"type": "boolean"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or replace a repository text file. Inspect existing files first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "Delete a regular repository file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_patch",
            "description": "Apply a unified git patch after a strict check.",
            "parameters": {
                "type": "object",
                "properties": {"patch": {"type": "string"}},
                "required": ["patch"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run an allowlisted Python quality/test command or read-only git command.",
            "parameters": {
                "type": "object",
                "properties": {
                    "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    "timeout": {"type": "integer", "minimum": 1, "maximum": 1200},
                },
                "required": ["argv"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": "Inspect the current repository diff.",
            "parameters": {
                "type": "object",
                "properties": {"staged": {"type": "boolean"}},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish_phase",
            "description": "Declare a phase complete after implementation and focused tests. The orchestrator still runs mandatory gates.",
            "parameters": {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
                "additionalProperties": False,
            },
        },
    },
]


def _request(model: str, messages: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    body = json.dumps(
        {
            "model": model,
            "messages": list(messages),
            "tools": TOOLS,
            "tool_choice": "auto",
            "temperature": 0.1,
            "max_tokens": 12000,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        ENDPOINT,
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer " + TOKEN,
            "Content-Type": "application/json",
            "Accept": "application/vnd.github+json",
            "User-Agent": "nanoAuralRuntime-P12-P14-engineer",
        },
    )
    delay = 2.0
    for attempt in range(8):
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            payload = error.read().decode("utf-8", "replace")
            if error.code in {404, 422}:
                raise AgentFailure(f"model {model} unavailable: {payload}") from error
            if error.code not in {408, 409, 429, 500, 502, 503, 504} or attempt == 7:
                raise AgentFailure(f"model request failed ({error.code}): {payload}") from error
            retry_after = error.headers.get("Retry-After")
            time.sleep(float(retry_after) if retry_after else delay)
            delay = min(delay * 2.0, 30.0)
        except (OSError, TimeoutError) as error:
            if attempt == 7:
                raise AgentFailure("model request repeatedly failed") from error
            time.sleep(delay)
            delay = min(delay * 2.0, 30.0)
    raise AgentFailure("model request exhausted retries")


def call_model(messages: Sequence[Mapping[str, Any]]) -> Tuple[str, Mapping[str, Any]]:
    failures: List[str] = []
    for model in MODEL_CANDIDATES:
        try:
            return model, _request(model, messages)
        except AgentFailure as error:
            failures.append(str(error))
    raise AgentFailure("; ".join(failures))


def _tool_result(name: str, arguments: Mapping[str, Any], state: MutableMapping[str, Any]) -> str:
    try:
        if name == "list_tree":
            return list_tree(**arguments)
        if name == "read_file":
            return read_file(**arguments)
        if name == "search_code":
            return search_code(**arguments)
        if name == "write_file":
            return write_file(**arguments)
        if name == "delete_file":
            return delete_file(**arguments)
        if name == "apply_patch":
            return apply_patch(**arguments)
        if name == "run_command":
            return run_command(**arguments)
        if name == "git_diff":
            return git_diff(**arguments)
        if name == "finish_phase":
            summary = arguments.get("summary")
            if not isinstance(summary, str) or not summary.strip():
                raise AgentFailure("phase summary is required")
            state["finished"] = True
            state["summary"] = summary.strip()
            return "phase completion recorded; mandatory gates will run next"
        raise AgentFailure("unknown tool")
    except (AgentFailure, OSError, UnicodeError, subprocess.SubprocessError, ValueError) as error:
        return "TOOL_ERROR: " + str(error)


def run_agent(label: str, prompt: str, max_rounds: int = 18) -> str:
    messages: List[Mapping[str, Any]] = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": prompt},
    ]
    state: Dict[str, Any] = {"finished": False, "summary": ""}
    no_tool_rounds = 0
    for round_number in range(1, max_rounds + 1):
        model, response = call_model(messages)
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            raise AgentFailure(f"{label}: model returned no choices")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise AgentFailure(f"{label}: malformed assistant message")
        assistant_message: Dict[str, Any] = {"role": "assistant"}
        if message.get("content") is not None:
            assistant_message["content"] = message.get("content")
        calls = message.get("tool_calls") or []
        if calls:
            assistant_message["tool_calls"] = calls
        messages.append(assistant_message)
        if not calls:
            no_tool_rounds += 1
            content = str(message.get("content") or "")
            diff_match = re.search(r"```diff\n(.*?)```", content, flags=re.DOTALL)
            if diff_match:
                result = apply_patch(diff_match.group(1))
                messages.append({"role": "user", "content": "Patch fallback result: " + result + ". Continue with tools."})
                no_tool_rounds = 0
            else:
                messages.append(
                    {
                        "role": "user",
                        "content": "Do not stop at prose. Inspect/edit/test through tools, then call finish_phase.",
                    }
                )
            if no_tool_rounds >= 3:
                raise AgentFailure(f"{label}: model repeatedly avoided tools ({model})")
            continue
        no_tool_rounds = 0
        for call in calls:
            call_id = call.get("id")
            function = call.get("function") or {}
            name = function.get("name")
            raw_arguments = function.get("arguments") or "{}"
            try:
                arguments = json.loads(raw_arguments)
                if not isinstance(arguments, dict):
                    raise ValueError
            except (json.JSONDecodeError, ValueError):
                result = "TOOL_ERROR: function arguments were not a JSON object"
            else:
                result = _tool_result(str(name), arguments, state)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": name,
                    "content": _trim(result),
                }
            )
        if state["finished"]:
            return str(state["summary"])
        if len(messages) > 54:
            messages = messages[:2] + messages[-48:]
    raise AgentFailure(f"{label}: exceeded {max_rounds} engineering rounds")


@dataclass(frozen=True)
class Phase:
    label: str
    commit: str
    prompt: str


PHASES = (
    Phase(
        "P12A",
        "feat: add durable artifact slot contracts",
        """Implement P12A completely. Introduce a strict stable ArtifactSlotId and immutable required slot contract; keep ArtifactKind only as a category. Persist required_artifact_slots on jobs and slot_id on publications/artifacts. Use (attempt_id, slot_id) uniqueness and exact required-slot-set finalization while preserving leases, cancellation, canonical VERIFIED checks and winning-attempt CAS. Replace the single-output planner with deterministic multi-artifact matching; legacy required_artifact_kinds and ProducedArtifact single output must map only at compatibility boundaries to output.primary / manifest.execution. Reject duplicates, missing/extra slots, ambiguous mappings, media/size/digest mismatches and booleans-as-integers. Add forward-only migration/backfill, API and remote client strict schemas, recovery support, package migration allowlists, documentation, unit and PostgreSQL tests. Inspect all existing code before editing and run focused tests.""",
    ),
    Phase(
        "P12B",
        "feat: stream durable artifact publication",
        """Implement P12B on top of P12A. Add model-neutral BytesArtifactSource and worker-owned FileArtifactSource plus bounded reader/chunk semantics and committed evidence. Evolve ProducedArtifact compatibly so durable production paths stream source -> controlled attempt ArtifactSink while incrementally computing SHA-256/size, validating limits and supporting abort/cleanup; no unbounded read, bytes(source), or model-output read_bytes in durable paths. Preserve the publication ledger states and multi-slot collective finalization. Update ControlFoley, Stable Audio 3 and Woosh outputs to file-backed sources with explicit workspace lifetime; retain explicit small/local bytes compatibility only at boundaries. Add multi-megabyte guarded-reader, mutation/truncation, cleanup, cancellation/stale lease, legacy and multi-slot tests and docs.""",
    ),
    Phase(
        "P12C",
        "feat: add resumable streaming asset uploads",
        """Implement P12C. Add first-class initiate/chunk/status/finalize/abort upload sessions with immutable expected size, SHA-256, media type, server-owned staging keys and a clearly enforced chunk offset/order policy. Identical replay is idempotent; conflicting overlap/range/oversize/truncation is rejected. Finalization streams the complete staged object, verifies exact size and SHA-256, applies media validation, atomically promotes and marks VERIFIED through PostgreSQL CAS. Add bounded streaming HTTP request handling and strict Content-Length/range limits, while legacy small bytes upload traverses the same state machine. Add remote upload_file/upload_stream with fixed chunks and resume/progress verification. Preserve namespace-hidden 404, prohibit client paths/keys, add expiry/abort cleanup, forward-only migrations/allowlists, restart/concurrency/PostgreSQL and guarded multi-megabyte tests.""",
    ),
    Phase(
        "P13",
        "feat: persist retry and session disposition contracts",
        """Implement P13. Define stable orthogonal RetryDisposition and SessionDisposition plus safe failure_code classification; do not infer both solely from exception class names. Map validation, deployment mismatch/unavailable, transient invocation, artifact source/validation/publication, session/process fatality, cancellation and operator-action failures centrally. Runtime session transitions obey SessionDisposition and poisoned sessions cannot be reused. Persist an immutable RetryPolicySnapshot per job (version, max attempts, bounded finite backoff, max delay, deterministic jitter policy/seed, retryable codes) copied from trusted server/deployment policy. Requeue/reaper/workers use the snapshot, not mutable process config. Expose sanitized stable status in API/remote responses. Add conservative migration backfill and exhaustive unit/PostgreSQL/session/restart/cancellation/backoff tests.""",
    ),
    Phase(
        "P14",
        "feat: seal deployments and enforce concurrency admission",
        """Implement P14. Add canonical versioned DeploymentSeal with deterministic JSON/SHA-256 covering adapter/version, task schemas, backend, runtime environment, source revision, weight/checkpoint and auxiliary manifests, precision/cache policy and all reproducibility identity. Make it the single internal authority; legacy fingerprints/fields are derived compatibility views. WorkerCapability must match the complete seal and declare device plus strict versioned ConcurrencyPolicy (SESSION/DEPLOYMENT/ADAPTER/DEVICE/PROCESS semantics, max_inflight, batching/max batch, admission key/resource class). Add admission control outside model-specific adapters with exception-safe permit release and safe single-flight defaults. Persist seal/policy snapshots in deployment/job/attempt provenance and include them in idempotency hashes without exposing operator paths. Add migration/backfill/allowlists, API/remote/plugin/builder/reference-worker changes, canonicalization/tamper/matching/admission/cancel/error/PostgreSQL tests and final architecture/operations docs.""",
    ),
)


def mandatory_gate() -> Tuple[bool, str]:
    commands = (
        (["python", "-m", "ruff", "format", "."], 300),
        (["python", "-m", "ruff", "check", "."], 300),
        (["python", "-m", "pyright"], 600),
        (["python", "-m", "pytest", "-q", "-m", "not gpu"], 1200),
    )
    log: List[str] = []
    for argv, timeout in commands:
        completed = _run(argv, timeout=timeout)
        log.append(
            "$ " + shlex.join(argv) + f"\nexit={completed.returncode}\n" + completed.stdout + completed.stderr
        )
        if completed.returncode:
            return False, _trim("\n\n".join(log), 50000)
    return True, _trim("\n\n".join(log), 50000)


def repair_gate(label: str, phase_prompt: str, failure: str) -> str:
    return run_agent(
        label + "-repair",
        """The mandatory software gate failed after this phase. Diagnose the actual code/test failure and fix production code/tests without weakening contracts, deleting tests, adding skips/xfails or broad catches. Re-run focused checks before finish_phase.\n\nPhase contract:\n{phase}\n\nGate output:\n{failure}""".format(
            phase=phase_prompt,
            failure=_trim(failure, 30000),
        ),
        max_rounds=12,
    )


def commit_phase(phase: Phase) -> str:
    _run(["git", "add", "-A"], timeout=60)
    staged = _run(["git", "diff", "--cached", "--quiet"], timeout=60)
    if staged.returncode == 0:
        raise AgentFailure(phase.label + " produced no committed implementation")
    committed = _run(["git", "commit", "-m", phase.commit], timeout=120)
    if committed.returncode:
        raise AgentFailure("commit failed:\n" + committed.stdout + committed.stderr)
    return _run(["git", "rev-parse", "HEAD"], timeout=30).stdout.strip()


def main() -> int:
    os.chdir(ROOT)
    phase_rows: List[Mapping[str, str]] = []
    gate_logs: List[str] = []
    for phase in PHASES:
        summary = run_agent(phase.label, phase.prompt)
        ok, gate = mandatory_gate()
        if not ok:
            repair_summary = repair_gate(phase.label, phase.prompt, gate)
            summary += "\nRepair: " + repair_summary
            ok, gate = mandatory_gate()
        if not ok:
            raise AgentFailure(phase.label + " gate remains red:\n" + gate)
        sha = commit_phase(phase)
        phase_rows.append({"phase": phase.label, "commit": sha, "summary": summary})
        gate_logs.append(phase.label + "\n" + gate)

    integration_prompt = """Perform a final independent integration review of P12A-P14 now present in git history. Inspect code and tests rather than trusting summaries. Repair any missing/unsafe integration, especially exact slot finalization, unbounded reads/whole-file copies, upload authority and cleanup, retry snapshot immutability/session poisoning, deployment seal single authority, complete capability matching, permit release, strict API types/unknown fields, forward-only migrations, package resources and security redaction. Run focused tests and finish only when no code change is still needed."""
    integration_summary = run_agent("integration", integration_prompt, max_rounds=16)
    ok, final_gate = mandatory_gate()
    if not ok:
        integration_summary += "\nRepair: " + repair_gate("integration", integration_prompt, final_gate)
        ok, final_gate = mandatory_gate()
    if not ok:
        raise AgentFailure("final integration gate remains red:\n" + final_gate)

    report = {
        "schema": "nano-aural-p12-p14-implementation-report-v1",
        "base": _run(["git", "rev-list", "--max-parents=0", "HEAD"], timeout=30).stdout.strip(),
        "phases": phase_rows,
        "integration_summary": integration_summary,
        "final_gate": final_gate,
        "migrations": sorted(
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / "src" / "nano_aural_runtime" / "durable" / "sql").glob("*.sql")
        ),
    }
    (ROOT / "P12_P14_IMPLEMENTATION_REPORT.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown = ["# P12-P14 Implementation Report", "", "## Phase commits", ""]
    for row in phase_rows:
        markdown.extend(
            [
                f"### {row['phase']} — `{row['commit']}`",
                "",
                str(row["summary"]),
                "",
            ]
        )
    markdown.extend(
        [
            "## Final integration review",
            "",
            integration_summary,
            "",
            "## Final mandatory gate",
            "",
            "```text",
            final_gate,
            "```",
            "",
            "## Migrations",
            "",
        ]
    )
    markdown.extend("- `" + item + "`" for item in report["migrations"])
    (ROOT / "P12_P14_IMPLEMENTATION_REPORT.md").write_text(
        "\n".join(markdown) + "\n", encoding="utf-8"
    )
    _run(["git", "add", "P12_P14_IMPLEMENTATION_REPORT.json", "P12_P14_IMPLEMENTATION_REPORT.md"])
    result = _run(["git", "commit", "-m", "docs: record P12-P14 implementation gates"], timeout=120)
    if result.returncode:
        raise AgentFailure("report commit failed:\n" + result.stdout + result.stderr)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AgentFailure, OSError, subprocess.SubprocessError, ValueError) as error:
        print("P12-P14 engineering failed: " + str(error), file=sys.stderr)
        raise SystemExit(1)
