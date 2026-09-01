# pyright: reportMissingImports=false
"""Static operations contracts for the Phase 3E Compose reference."""

from __future__ import annotations

import json
import math
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest

from nano_aural_runtime.durable.artifact_publication import PublicationFaultPoint
from nano_aural_runtime.durable.recovery import _dry_run_attempts
from nano_aural_runtime.durable.reference_worker import ReferenceWorkerConfig

ROOT = Path(__file__).resolve().parents[1]


def test_compose_reference_is_parseable_secret_backed_and_profile_explicit() -> None:
    compose = json.loads((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert {
        "postgres",
        "migrate",
        "grant-runtime-privileges",
        "api",
        "cpu-reference-worker",
        "gpu-worker-placeholder",
    }.issubset(services)
    assert services["postgres"]["image"] == (
        "postgres:16.3-bookworm@sha256:"
        "d0f363f8366fbc3f52d172c6e76bc27151c3d643b870e1062b4e8bfe65baf609"
    )
    assert services["postgres"]["environment"]["POSTGRES_USER"] == "nano_aural_migrator"
    assert services["migrate"]["command"][-1] == "--migrate-only"
    assert services["grant-runtime-privileges"]["command"][-2:] == [
        "--grant-runtime-role",
        "nano_aural_runtime",
    ]
    assert services["grant-runtime-privileges"]["depends_on"]["migrate"]["condition"] == (
        "service_completed_successfully"
    )
    assert services["api"]["depends_on"]["grant-runtime-privileges"]["condition"] == (
        "service_completed_successfully"
    )
    assert (
        services["cpu-reference-worker"]["depends_on"]["grant-runtime-privileges"]["condition"]
        == "service_completed_successfully"
    )
    assert services["migrate"]["secrets"] == ["migration_database_dsn"]
    assert services["grant-runtime-privileges"]["secrets"] == ["migration_database_dsn"]
    assert "runtime_database_dsn" not in services["migrate"]["secrets"]
    assert "runtime_database_dsn" not in services["grant-runtime-privileges"]["secrets"]
    assert "runtime_database_dsn" in services["api"]["secrets"]
    assert "migration_database_dsn" not in services["api"]["secrets"]
    assert services["cpu-reference-worker"]["secrets"] == ["runtime_database_dsn"]
    assert services["api"]["environment"]["NANO_AURAL_RUNTIME_DATABASE_DSN_FILE"] == (
        "/run/secrets/runtime_database_dsn"
    )
    assert services["migrate"]["environment"]["NANO_AURAL_MIGRATION_DATABASE_DSN_FILE"] == (
        "/run/secrets/migration_database_dsn"
    )
    assert set(services["postgres"]["secrets"]) == {
        "postgres_migration_password",
        "postgres_runtime_password",
    }
    assert services["postgres"]["entrypoint"] == ["/opt/nano-aural/postgres-secret-entrypoint.sh"]
    assert any(
        "postgres-secret-entrypoint.sh" in volume for volume in services["postgres"]["volumes"]
    )
    assert any("init-runtime-role.sh" in volume for volume in services["postgres"]["volumes"])
    assert any("/run/nano-aural-secrets" in mount for mount in services["postgres"]["tmpfs"])
    assert "healthcheck" in services["postgres"]
    assert "healthcheck" in services["api"]
    assert "healthcheck" in services["cpu-reference-worker"]
    assert services["gpu-worker-placeholder"]["profiles"] == ["gpu-deferred"]
    assert "deferred" in services["gpu-worker-placeholder"]["command"][-1].lower()
    for service in (
        "migrate",
        "grant-runtime-privileges",
        "api",
        "cpu-reference-worker",
        "gpu-worker-placeholder",
    ):
        assert services[service]["platform"] == "linux/amd64"
    assert set(compose["volumes"]) == {
        "postgres-data",
        "canonical-data",
        "staging-data",
        "attempt-data",
    }
    assert set(compose["secrets"]) == {
        "postgres_migration_password",
        "postgres_runtime_password",
        "migration_database_dsn",
        "runtime_database_dsn",
        "token_grants",
    }
    serialized = json.dumps(compose, sort_keys=True)
    assert "postgresql://" not in serialized
    assert "Bearer " not in serialized
    assert "token_sha256" not in serialized


def test_api_image_is_model_agnostic_and_has_minimal_dependencies() -> None:
    dockerfile = (ROOT / "ops" / "Dockerfile.api").read_text(encoding="utf-8")
    executable = "\n".join(
        line for line in dockerfile.splitlines() if not line.lstrip().startswith("#")
    ).casefold()
    requirements = (ROOT / "ops" / "requirements-api.txt").read_text(encoding="utf-8").casefold()
    assert "copy src/nano_aural_runtime " in executable
    for prohibited in ("controlfoley", "torch", "cuda", "comfyui", "triton"):
        assert prohibited not in executable
        assert prohibited not in requirements
    assert executable.startswith(
        "from python:3.12-slim-bookworm@sha256:"
        "d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b"
    )
    assert "pip install --no-cache-dir --require-hashes --only-binary=:all:" in executable
    assert requirements.startswith("--require-hashes\n--only-binary=:all:\n")
    expected_requirements = {
        "psycopg[binary]==3.2.13": (
            "a481374514f2da627157f767a9336705ebefe93ea7a0522a6cbacba165da179a"
        ),
        "psycopg-binary==3.2.13": (
            "8f1189dc78553ef4b2e55d9e116fc74870191bc6a9a5f4442412a703c4cc6c3b"
        ),
        "typing_extensions==4.15.0": (
            "f0fa19c6845758ab08074a0cfa8b7aecb71c999ca73d62883bc25cc018c4e548"
        ),
    }
    for requirement, archive_hash in expected_requirements.items():
        assert requirement in requirements
        assert "--hash=sha256:" + archive_hash in requirements

    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert dockerignore.splitlines()[0] == "**"
    for excluded in (
        ".git",
        ".venv",
        "benchmarks",
        "docs/source-plans",
        "weights",
        "*.wav",
        ".env",
        "secrets",
    ):
        assert excluded in dockerignore
    for allowed in (
        "!ops/Dockerfile.api",
        "!ops/requirements-api.txt",
        "!ops/load_secrets.py",
        "!src/nano_aural_runtime/**",
    ):
        assert allowed in dockerignore


def test_postgres_runtime_role_init_uses_owner_only_secret_without_password_argv() -> None:
    entrypoint_path = ROOT / "ops" / "postgres-secret-entrypoint.sh"
    entrypoint = entrypoint_path.read_text(encoding="utf-8")
    assert entrypoint_path.stat().st_mode & 0o777 == 0o755
    assert "source_secret=/run/secrets/postgres_runtime_password" in entrypoint
    assert "400|600" in entrypoint
    assert "bs=4097 count=1" in entrypoint
    assert "install -d -o 0 -g 999 -m 0710" in entrypoint
    assert "chown 999:999" in entrypoint
    assert "chmod 0400" in entrypoint
    assert 'exec /usr/local/bin/docker-entrypoint.sh "$@"' in entrypoint
    assert "set -x" not in entrypoint

    script_path = ROOT / "ops" / "init-runtime-role.sh"
    script = script_path.read_text(encoding="utf-8")
    assert script_path.stat().st_mode & 0o777 == 0o755
    assert "secret=/run/nano-aural-secrets/postgres_runtime_password" in script
    assert "stat -c '%u'" in script
    assert "stat -c '%a'" in script
    assert "!= 400" in script
    assert "\\getenv runtime_password NANO_AURAL_RUNTIME_PASSWORD" in script
    assert ":'runtime_password'" in script
    assert "--set=runtime_password" not in script
    assert "set -x" not in script
    assert "PASSWORD %L" in script


@pytest.mark.parametrize(
    "trailing",
    (b"\ntrailing-without-newline", b"\ntrailing-with-newline\n", b"\n\n"),
    ids=("unterminated-second-line", "terminated-second-line", "empty-second-line"),
)
def test_postgres_runtime_role_init_rejects_any_trailing_line(
    tmp_path: Path,
    trailing: bytes,
) -> None:
    canary = "runtime-secret-canary"
    secret = tmp_path / "runtime-password"
    secret.write_bytes(canary.encode() + trailing)
    secret.chmod(0o400)

    script_source = (ROOT / "ops" / "init-runtime-role.sh").read_text(encoding="utf-8")
    script_source = script_source.replace(
        "secret=/run/nano-aural-secrets/postgres_runtime_password",
        f"secret={shlex.quote(str(secret))}",
        1,
    )
    script = tmp_path / "init-runtime-role.sh"
    script.write_text(script_source, encoding="utf-8")
    script.chmod(0o700)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_stat = fake_bin / "stat"
    fake_stat.write_text(
        "#!/bin/sh\n"
        'case "$2" in\n'
        "  %u) id -u ;;\n"
        "  %a) printf '400\\n' ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_stat.chmod(0o700)
    fake_psql = fake_bin / "psql"
    fake_psql.write_text(
        '#!/bin/sh\n: > "$NANO_AURAL_TEST_PSQL_MARKER"\n',
        encoding="utf-8",
    )
    fake_psql.chmod(0o700)
    marker = tmp_path / "psql-ran"
    environment = {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "POSTGRES_DB": "nano_aural",
        "POSTGRES_USER": "nano_aural_migrator",
        "NANO_AURAL_TEST_PSQL_MARKER": str(marker),
    }

    completed = subprocess.run(
        [str(script)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=5,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr == "runtime role initialization failed; check mounted secret\n"
    assert canary not in completed.stderr
    assert not marker.exists()


def test_api_and_ops_entry_imports_do_not_load_model_or_ui_packages() -> None:
    code = """
import sys
import nano_aural_runtime.durable.service
import nano_aural_runtime.durable.reference_worker
import nano_aural_runtime.durable.recovery
for name in sys.modules:
    assert not name.startswith(('torch', 'comfyui', 'nano_aural_runtime_controlfoley'))
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT / "src")
    subprocess.run([sys.executable, "-c", code], check=True, env=environment, timeout=10)


def test_all_ops_help_paths_need_no_database_or_secret() -> None:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("NANO_AURAL_")
    }
    environment["PYTHONPATH"] = str(ROOT / "src")
    commands = (
        ("-m", "nano_aural_runtime.durable.service", "--help"),
        ("-m", "nano_aural_runtime.durable.reference_worker", "--help"),
        ("-m", "nano_aural_runtime.durable.recovery", "--help"),
        (str(ROOT / "ops" / "load_secrets.py"), "--help"),
        ("-m", "nano_aural_runtime.durable.migration_admin", "--help"),
    )
    for arguments in commands:
        completed = subprocess.run(
            [sys.executable, *arguments],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
            timeout=10,
        )
        assert "usage:" in completed.stdout
        assert "postgresql://" not in completed.stdout + completed.stderr


def test_reference_config_hides_dsn_and_document_covers_recovery_matrix(tmp_path: Path) -> None:
    deployment_id, worker_id = str(uuid4()), str(uuid4())
    dsn = "postgresql://private.invalid/reference"
    config = ReferenceWorkerConfig.from_environment(
        {
            "NANO_AURAL_DATABASE_DSN": dsn,
            "NANO_AURAL_CANONICAL_BLOB_ROOT": str(tmp_path / "canonical"),
            "NANO_AURAL_ATTEMPT_ARTIFACT_ROOT": str(tmp_path / "attempts"),
            "NANO_AURAL_WORKSPACE_ROOT": str(tmp_path / "workspaces"),
            "NANO_AURAL_REFERENCE_DEPLOYMENT_ID": deployment_id,
            "NANO_AURAL_REFERENCE_WORKER_ID": worker_id,
        }
    )
    assert dsn not in repr(config)
    document = (ROOT / "docs" / "durable-operations.md").read_text(encoding="utf-8")
    for point in PublicationFaultPoint:
        assert "`{0}`".format(point.value) in document
    for phrase in (
        "过期前上传卡在 `VERIFYING`",
        "取消与执行/发布竞态",
        "回收器与心跳竞态",
        "Mandatory dry run",
        "有意没有规范 blob 删除命令",
        "4090 冒烟/恢复仍为 **DEFERRED**",
        "结构化事件字段",
        "全文件下载 SHA-256",
    ):
        assert phrase in document


def test_reference_config_rejects_non_finite_idle_interval(tmp_path: Path) -> None:
    base = {
        "database_dsn": "postgresql://private.invalid/reference",
        "canonical_blob_root": tmp_path / "canonical",
        "attempt_artifact_root": tmp_path / "attempts",
        "workspace_root": tmp_path / "workspaces",
        "deployment_id": str(uuid4()),
        "worker_id": str(uuid4()),
    }
    for value in (math.nan, math.inf, -math.inf):
        try:
            ReferenceWorkerConfig(**base, idle_seconds=value)
        except ValueError:
            pass
        else:
            raise AssertionError("non-finite idle interval was accepted")


def _run_secret_loader(
    environment: dict[str, str], timeout: float = 10
) -> subprocess.CompletedProcess[str]:
    code = """
import json
import os
print(json.dumps({key: value for key, value in os.environ.items() if key.startswith('NANO_AURAL_')}, sort_keys=True))
"""
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "ops" / "load_secrets.py"),
            sys.executable,
            "-c",
            code,
        ],
        capture_output=True,
        text=True,
        env=environment,
        timeout=timeout,
    )


def test_secret_loader_execs_with_only_allowlisted_secret_targets(tmp_path: Path) -> None:
    dsn = "postgresql://operator:private-value@postgres/service"
    grants = '[{"token_sha256":"' + "a" * 64 + '"}]'
    dsn_file = tmp_path / "database_dsn"
    grants_file = tmp_path / "token_grants"
    dsn_file.write_text(dsn + "\n", encoding="utf-8")
    grants_file.write_text(grants, encoding="utf-8")

    completed = _run_secret_loader(
        {
            "NANO_AURAL_RUNTIME_DATABASE_DSN_FILE": str(dsn_file),
            "NANO_AURAL_TOKEN_GRANTS_JSON_FILE": str(grants_file),
        }
    )

    assert completed.returncode == 0
    assert completed.stderr == ""
    assert json.loads(completed.stdout) == {
        "NANO_AURAL_DATABASE_DSN": dsn,
        "NANO_AURAL_TOKEN_GRANTS_JSON": grants,
    }

    completed = _run_secret_loader({"NANO_AURAL_MIGRATION_DATABASE_DSN_FILE": str(dsn_file)})
    assert completed.returncode == 0
    assert json.loads(completed.stdout) == {"NANO_AURAL_DATABASE_DSN": dsn}


def test_secret_loader_rejects_unsafe_sources_without_disclosure(tmp_path: Path) -> None:
    secret_value = "do-not-disclose-private-secret"
    regular = tmp_path / "regular"
    regular.write_text(secret_value, encoding="utf-8")
    symlink = tmp_path / "symlink"
    symlink.symlink_to(regular)
    directory = tmp_path / "directory"
    directory.mkdir()
    oversized = tmp_path / "oversized"
    oversized.write_bytes(b"x" * (64 * 1024 + 1))
    invalid_utf8 = tmp_path / "invalid-utf8"
    invalid_utf8.write_bytes(b"\xff")
    multiline = tmp_path / "multiline"
    multiline.write_text("first\nsecond", encoding="utf-8")

    cases = (
        ({"NANO_AURAL_DATABASE_DSN_FILE": str(symlink)}, (symlink, regular)),
        ({"NANO_AURAL_DATABASE_DSN_FILE": str(directory)}, (directory,)),
        ({"NANO_AURAL_DATABASE_DSN_FILE": str(oversized)}, (oversized,)),
        ({"NANO_AURAL_DATABASE_DSN_FILE": str(invalid_utf8)}, (invalid_utf8,)),
        ({"NANO_AURAL_DATABASE_DSN_FILE": str(multiline)}, (multiline,)),
        (
            {
                "NANO_AURAL_DATABASE_DSN_FILE": str(regular),
                "NANO_AURAL_DATABASE_DSN": secret_value,
            },
            (regular,),
        ),
        (
            {
                "NANO_AURAL_MIGRATION_DATABASE_DSN_FILE": str(regular),
                "NANO_AURAL_RUNTIME_DATABASE_DSN_FILE": str(regular),
            },
            (regular,),
        ),
    )
    for environment, paths in cases:
        completed = _run_secret_loader(environment)
        assert completed.returncode == 2
        assert completed.stdout == ""
        assert completed.stderr == "secret loading failed; check mounted secret files\n"
        assert secret_value not in completed.stderr
        for path in paths:
            assert str(path) not in completed.stderr


def test_secret_loader_rejects_fifo_without_blocking_or_disclosure(tmp_path: Path) -> None:
    fifo = tmp_path / "mounted-secret-fifo"
    os.mkfifo(fifo)
    started = time.monotonic()
    completed = _run_secret_loader({"NANO_AURAL_DATABASE_DSN_FILE": str(fifo)}, timeout=1)
    elapsed = time.monotonic() - started
    assert elapsed < 1
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "secret loading failed; check mounted secret files\n"
    assert str(fifo) not in completed.stderr


def test_dry_run_stops_when_cleanup_candidates_exhaust_limit(tmp_path: Path) -> None:
    class FullCleanupPage:
        def cleanup_candidates(self, before: object, limit: int) -> tuple[object, ...]:
            assert before is not None
            assert limit == 1
            return (SimpleNamespace(publication_id="publication-1"),)

        def orphan_keys_before(self, before: object, limit: int) -> tuple[str, ...]:
            raise AssertionError("zero-slot orphan query must not run")

    with (
        patch(
            "nano_aural_runtime.durable.recovery.PostgresPublicationRepository",
            return_value=FullCleanupPage(),
        ),
        patch(
            "nano_aural_runtime.durable.recovery.LocalAttemptArtifactStore.inventory_page_before",
            side_effect=AssertionError("zero-slot inventory must not run"),
        ),
    ):
        assert (
            _dry_run_attempts(
                object(),
                tmp_path / "attempts",
                datetime.now(timezone.utc),
                1,
            )
            == 1
        )
