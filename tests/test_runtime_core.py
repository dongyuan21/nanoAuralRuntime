"""CPU-only tests for the model-agnostic Runtime Core.

The test module uses ``unittest`` so it can run on a fresh Python installation;
pytest can collect it unchanged when the optional development dependency exists.
"""

from __future__ import annotations

import threading
import time
import unittest
from typing import Any, cast

from nano_aural_runtime import (
    AdapterContractError,
    AdapterExecutionError,
    AdapterNotFoundError,
    AdapterRegistrationError,
    AdapterRegistry,
    CacheReport,
    CancellationToken,
    EchoAdapter,
    ExecutionContext,
    FakeAudioAdapter,
    InvocationCancelledError,
    InvocationRejectedError,
    InvocationResult,
    ModelDeployment,
    ModelDescriptor,
    ModelInvocation,
    ProducedArtifact,
    ProfileReport,
    Runtime,
    SessionState,
    SessionStateError,
)


def deployment_for(adapter_id: str) -> ModelDeployment:
    return ModelDeployment(
        deployment_id="{0}-deployment".format(adapter_id),
        descriptor=ModelDescriptor(adapter_id=adapter_id, model_id="unit", version="1"),
        fingerprint="unit-fingerprint",
    )


class RuntimeCoreTests(unittest.TestCase):
    def test_load_invoke_unload_echo(self) -> None:
        registry = AdapterRegistry()
        registry.register(EchoAdapter())
        runtime = Runtime(registry)

        session = runtime.load(deployment_for("echo"))
        self.assertEqual(SessionState.READY, session.state)

        result = runtime.invoke(
            session,
            ModelInvocation("invoke-1", "echo", inputs={"value": "hello"}),
        )
        self.assertEqual("invoke-1", result.invocation_id)
        self.assertEqual(1, len(result.artifacts))
        self.assertEqual("text/plain", result.artifacts[0].media_type)
        self.assertEqual(b"hello", result.artifacts[0].content)
        self.assertEqual(SessionState.READY, session.state)

        runtime.unload(session)
        self.assertEqual(SessionState.UNLOADED, session.state)

    def test_result_has_empty_model_neutral_reports_by_default(self) -> None:
        result = InvocationResult("result-1")
        self.assertEqual(ProfileReport(), result.profile)
        self.assertEqual(CacheReport(), result.cache)

    def test_adapter_lookup_failure(self) -> None:
        with self.assertRaises(AdapterNotFoundError):
            Runtime().load(deployment_for("missing"))

    def test_load_requires_model_deployment(self) -> None:
        with self.assertRaises(TypeError):
            Runtime().load(cast(Any, object()))

    def test_load_failure_marks_captured_session_failed(self) -> None:
        class LoadFailureAdapter(FakeAudioAdapter):
            _descriptor = ModelDescriptor("load-failure", "unit", "1")

            def __init__(self):
                super().__init__()
                self.captured_session = None

            def load(self, session):
                self.captured_session = session
                raise RuntimeError("load failed")

        adapter = LoadFailureAdapter()
        registry = AdapterRegistry()
        registry.register(adapter)
        with self.assertRaisesRegex(RuntimeError, "load failed"):
            Runtime(registry).load(deployment_for("load-failure"))
        captured_session = adapter.captured_session
        self.assertIsNotNone(captured_session)
        assert captured_session is not None
        self.assertEqual(SessionState.FAILED, captured_session.state)

    def test_multiple_artifact_media_types_are_preserved(self) -> None:
        def make_result(session, invocation, context):
            return InvocationResult(
                invocation_id=invocation.invocation_id,
                artifacts=(
                    ProducedArtifact("one.txt", "text/plain", b"one"),
                    ProducedArtifact("two.bin", "application/octet-stream", b"two"),
                ),
            )

        adapter = FakeAudioAdapter(result_factory=make_result)
        registry = AdapterRegistry()
        registry.register(adapter)
        runtime = Runtime(registry)
        session = runtime.load(deployment_for("fake"))

        result = runtime.invoke(session, ModelInvocation("invoke-2", "test"))
        self.assertEqual(
            ["text/plain", "application/octet-stream"],
            [artifact.media_type for artifact in result.artifacts],
        )
        self.assertEqual(1, adapter.invoke_calls)

    def test_registry_rejects_duplicate_adapter_ids(self) -> None:
        registry = AdapterRegistry()
        registry.register(EchoAdapter())
        with self.assertRaises(AdapterRegistrationError):
            registry.register(EchoAdapter())

    def test_rejected_invocation_keeps_session_ready(self) -> None:
        def reject(session, invocation, context):
            raise InvocationRejectedError("unsupported input")

        registry = AdapterRegistry()
        registry.register(FakeAudioAdapter(result_factory=reject))
        runtime = Runtime(registry)
        session = runtime.load(deployment_for("fake"))
        with self.assertRaises(InvocationRejectedError):
            runtime.invoke(session, ModelInvocation("rejected", "test"))
        self.assertEqual(SessionState.READY, session.state)

    def test_cancellation_during_adapter_execution_keeps_session_ready(self) -> None:
        entered = []

        def cancel_during_execution(session, invocation, context):
            entered.append(invocation.invocation_id)
            context.cancellation_token.cancel("adapter requested cancellation")
            return InvocationResult(invocation.invocation_id)

        registry = AdapterRegistry()
        registry.register(FakeAudioAdapter(result_factory=cancel_during_execution))
        runtime = Runtime(registry)
        session = runtime.load(deployment_for("fake"))
        with self.assertRaises(InvocationCancelledError):
            runtime.invoke(session, ModelInvocation("cancelled", "test"))
        self.assertEqual(["cancelled"], entered)
        self.assertEqual(SessionState.READY, session.state)

    def test_unknown_invoke_fault_marks_failed_and_can_unload(self) -> None:
        def fail(session, invocation, context):
            raise RuntimeError("unexpected adapter fault")

        registry = AdapterRegistry()
        registry.register(FakeAudioAdapter(result_factory=fail))
        runtime = Runtime(registry)
        session = runtime.load(deployment_for("fake"))
        with self.assertRaisesRegex(AdapterExecutionError, "adapter invocation failed") as caught:
            runtime.invoke(session, ModelInvocation("fault", "test"))
        self.assertIsInstance(caught.exception.__cause__, RuntimeError)
        self.assertEqual("unexpected adapter fault", str(caught.exception.__cause__))
        self.assertEqual(SessionState.FAILED, session.state)
        runtime.unload(session)
        self.assertEqual(SessionState.UNLOADED, session.state)

    def test_invalid_result_contract_marks_session_failed(self) -> None:
        def invalid_type(session, invocation, context):
            return cast(InvocationResult, object())

        registry = AdapterRegistry()
        registry.register(FakeAudioAdapter(result_factory=invalid_type))
        runtime = Runtime(registry)
        session = runtime.load(deployment_for("fake"))
        with self.assertRaises(AdapterContractError):
            runtime.invoke(session, ModelInvocation("bad-type", "test"))
        self.assertEqual(SessionState.FAILED, session.state)

    def test_mismatched_result_id_marks_session_failed(self) -> None:
        def invalid_id(session, invocation, context):
            return InvocationResult("other-invocation")

        registry = AdapterRegistry()
        registry.register(FakeAudioAdapter(result_factory=invalid_id))
        runtime = Runtime(registry)
        session = runtime.load(deployment_for("fake"))
        with self.assertRaises(AdapterContractError):
            runtime.invoke(session, ModelInvocation("expected-invocation", "test"))
        self.assertEqual(SessionState.FAILED, session.state)

    def test_unload_failure_marks_session_failed(self) -> None:
        class UnloadFailureAdapter(FakeAudioAdapter):
            _descriptor = ModelDescriptor("unload-failure", "unit", "1")

            def unload(self, session):
                raise RuntimeError("unload failed")

        registry = AdapterRegistry()
        registry.register(UnloadFailureAdapter())
        runtime = Runtime(registry)
        session = runtime.load(deployment_for("unload-failure"))
        with self.assertRaisesRegex(RuntimeError, "unload failed"):
            runtime.unload(session)
        self.assertEqual(SessionState.FAILED, session.state)

    def test_unloaded_deployment_can_be_loaded_into_a_new_session(self) -> None:
        adapter = FakeAudioAdapter()
        registry = AdapterRegistry()
        registry.register(adapter)
        runtime = Runtime(registry)
        deployment = deployment_for("fake")

        first = runtime.load(deployment)
        runtime.unload(first)
        second = runtime.load(deployment)

        self.assertNotEqual(first.session_id, second.session_id)
        self.assertEqual(SessionState.UNLOADED, first.state)
        self.assertEqual(SessionState.READY, second.state)
        self.assertEqual(2, adapter.load_calls)

    def test_value_metadata_is_defensively_immutable(self) -> None:
        source = {"nested": {"items": ["first"]}}
        invocation = ModelInvocation("immutable", "test", metadata=source)
        source["nested"]["items"].append("later")

        self.assertEqual(("first",), invocation.metadata["nested"]["items"])
        with self.assertRaises(TypeError):
            cast(Any, invocation.metadata)["extra"] = "not allowed"

    def test_unload_is_rejected_while_session_is_running(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def block(session, invocation, context):
            started.set()
            release.wait(timeout=2)
            return InvocationResult(invocation.invocation_id)

        registry = AdapterRegistry()
        registry.register(FakeAudioAdapter(result_factory=block))
        runtime = Runtime(registry)
        session = runtime.load(deployment_for("fake"))
        worker = threading.Thread(
            target=runtime.invoke,
            args=(session, ModelInvocation("running", "test")),
        )
        worker.start()
        self.assertTrue(started.wait(timeout=1))
        with self.assertRaises(SessionStateError):
            runtime.unload(session)
        release.set()
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(SessionState.READY, session.state)

    def test_same_session_invocations_are_serialized(self) -> None:
        first_started = threading.Event()
        permit_first_finish = threading.Event()
        second_started = threading.Event()
        call_order = []
        lock = threading.Lock()

        def block_first(session, invocation, context):
            with lock:
                call_order.append(invocation.invocation_id)
            if invocation.invocation_id == "first":
                first_started.set()
                permit_first_finish.wait(timeout=2)
            else:
                second_started.set()
            return InvocationResult(invocation.invocation_id)

        registry = AdapterRegistry()
        registry.register(FakeAudioAdapter(result_factory=block_first))
        runtime = Runtime(registry)
        session = runtime.load(deployment_for("fake"))
        first = threading.Thread(
            target=runtime.invoke,
            args=(session, ModelInvocation("first", "test")),
        )
        second = threading.Thread(
            target=runtime.invoke,
            args=(session, ModelInvocation("second", "test")),
        )

        first.start()
        self.assertTrue(first_started.wait(timeout=1))
        second.start()
        time.sleep(0.1)
        self.assertFalse(
            second_started.is_set(), "second invocation entered adapter while first ran"
        )

        permit_first_finish.set()
        first.join(timeout=1)
        second.join(timeout=1)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertTrue(second_started.is_set())
        self.assertEqual(["first", "second"], call_order)
        self.assertEqual(SessionState.READY, session.state)

    def test_cancelled_waiter_never_enters_adapter(self) -> None:
        first_started = threading.Event()
        permit_first_finish = threading.Event()
        entered = []

        def block(session, invocation, context):
            entered.append(invocation.invocation_id)
            if invocation.invocation_id == "first":
                first_started.set()
                permit_first_finish.wait(timeout=2)
            return InvocationResult(invocation.invocation_id)

        registry = AdapterRegistry()
        registry.register(FakeAudioAdapter(result_factory=block))
        runtime = Runtime(registry)
        session = runtime.load(deployment_for("fake"))
        first = threading.Thread(
            target=runtime.invoke,
            args=(session, ModelInvocation("first", "test")),
        )
        first.start()
        self.assertTrue(first_started.wait(timeout=1))

        token = CancellationToken()
        context = ExecutionContext(cancellation_token=token)
        errors = []

        def cancelled_waiter():
            try:
                runtime.invoke(session, ModelInvocation("second", "test"), context)
            except InvocationCancelledError as error:
                errors.append(error)

        second = threading.Thread(target=cancelled_waiter)
        second.start()
        time.sleep(0.1)
        token.cancel("test cancellation")
        second.join(timeout=1)
        self.assertFalse(second.is_alive())
        self.assertEqual(1, len(errors))
        self.assertEqual(["first"], entered)

        permit_first_finish.set()
        first.join(timeout=1)
        self.assertEqual(SessionState.READY, session.state)


if __name__ == "__main__":
    unittest.main()
