"""Provider, PCM snapshot, fallback and receipt contracts; no real network."""
from tests import isolation  # noqa: F401
import asyncio
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import numpy as np
import soundfile as sf

from qwen_asr_app import inference_providers as providers
from qwen_asr_app import remote_operations as operations
from qwen_asr_app.durable import atomic_json
from qwen_asr_app.model_runtime import ModelManager


class ProvenNotAccepted(RuntimeError):
    proven_not_accepted = True


class ReceiptContractTests(unittest.TestCase):
    def test_missing_receipt_means_no_existing_operation(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(operations.receipt(Path(tmp) / "missing.json"), {})
            self.assertEqual(operations.receipt(None), {})

    def test_receipt_wrong_json_shapes_require_repair(self):
        values = [[], None, "completed", 42, True, {"state": []}, {"state": None}, {"state": 42}]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "receipt.json"
            for value in values:
                with self.subTest(value=value):
                    path.write_text(json.dumps(value), encoding="utf-8")
                    self.assertEqual(operations.receipt(path), {
                        "state": "receipt_corrupt", "recovery_required": True,
                    })
                    with self.assertRaises(operations.ResultNeedsRepair):
                        with operations.accepted_result_guard(path):
                            raise ValueError("invalid result")
                    with self.assertRaises(ValueError):
                        operations.mark_local_fallback(path)

    def test_broken_json_and_unreadable_receipt_require_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "receipt.json"
            path.write_text("{", encoding="utf-8")
            self.assertEqual(operations.receipt(path)["state"], "receipt_corrupt")
            self.assertTrue(operations.receipt(Path(tmp))["recovery_required"])

    def test_valid_receipt_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "receipt.json"
            value = {"state": "downloaded", "job_id": "unit-test", "metrics": {"seconds": 1}}
            atomic_json(path, value)
            self.assertEqual(operations.receipt(path), value)
            atomic_json(path, {})
            self.assertEqual(operations.receipt(path), {})

    def test_accepted_result_errors_cannot_enable_recomputation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "receipt.json"
            for state in ("submitted", "completed", "downloaded", "receipt_corrupt"):
                with self.subTest(state=state):
                    atomic_json(path, {"state": state})
                    with self.assertRaises(operations.ResultNeedsRepair) as caught:
                        with operations.accepted_result_guard(path):
                            raise ValueError("bad output")
                    self.assertTrue(caught.exception.fallback_unsafe)
                    self.assertIsInstance(caught.exception.__cause__, ValueError)

    def test_only_proven_nonacceptance_can_mark_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "receipt.json"
            atomic_json(path, {"state": "proven_not_accepted", "key": "same-operation"})
            operations.mark_local_fallback(path)
            self.assertEqual(operations.receipt(path), {"state": "local_fallback", "key": "same-operation"})
            operations.mark_local_fallback(path)
            atomic_json(path, {"state": "completed"})
            with self.assertRaises(ValueError):
                operations.mark_local_fallback(path)

    def test_cancellation_intent_survives_until_confirmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "receipt.json"
            self.assertFalse(operations.cancel_pending(path))
            operations.cancel(path)
            self.assertTrue(operations.cancel_pending(path))
            atomic_json(path, {"state": "canceled", "cancel_resolved": True})
            self.assertFalse(operations.cancel_pending(path))

    def test_snapshots_are_deep_copied_and_secrets_are_not_persisted(self):
        original = {"nested": [{"api_key": "secret", "Authorization": "secret", "value": [1]}]}
        saved = operations.without_secrets(original)
        self.assertEqual(saved, {"nested": [{"value": [1]}]})
        saved["nested"][0]["value"].append(2)
        self.assertEqual(original["nested"][0]["value"], [1])
        self.assertEqual(operations.saved_config()["asr_provider"], "local")
        with self.assertRaises(ValueError):
            operations.saved_config(remote_reference=True)


class AudioSnapshotTests(unittest.TestCase):
    def test_pcm_sample_rate_hash_and_request_are_consistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            samples = np.linspace(-.5, .5, 1600, dtype=np.float32)
            request = providers.prepare_audio(folder, samples, "Chinese", "unit-test", 512, {})
            audio, sample_rate = sf.read(folder / "audio.wav")
            self.assertEqual(sample_rate, 16000)
            self.assertEqual(len(audio), len(samples))
            np.testing.assert_allclose(audio, samples, atol=1 / 32768)
            self.assertEqual(request["source_sha256"], hashlib.sha256((folder / "audio.wav").read_bytes()).hexdigest())
            self.assertEqual(request["duration"], .1)
            self.assertEqual(request["max_new_tokens"], 512)

    def test_existing_snapshot_is_reused_without_rewriting_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            first = providers.prepare_audio(folder, np.zeros(160), "Chinese", "saved", 512,
                {"token": "never-persist", "nested": {"api_key": "never-persist"}}, {"max_tokens": 1024})
            audio = (folder / "audio.wav").read_bytes()
            restored = providers.prepare_audio(folder, np.ones(320), "English", "changed", 128, {},
                {"temperature": 1.0})
            self.assertEqual(first, restored)
            self.assertEqual((folder / "audio.wav").read_bytes(), audio)
            self.assertNotIn("never-persist", (folder / "request.json").read_text())
            self.assertEqual(restored["max_new_tokens"], 1024)

    def test_missing_snapshot_with_existing_receipt_is_not_resubmitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            atomic_json(folder / "receipt.json", {"state": "completed"})
            with self.assertRaises(ValueError):
                providers.prepare_audio(folder, [0.0] * 160, None, "", 384, {})
            self.assertFalse((folder / "audio.wav").exists())

    def test_remote_unsupported_custom_parameters_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            for params in ({"temperature": 1.0}, {"top_k": 10}, {"max_tokens": 64}):
                with self.subTest(params=params):
                    with self.assertRaises(ValueError):
                        providers.prepare_audio(Path(tmp), [0.0] * 160, None, "", 384, {}, params)


class ProviderRoutingTests(unittest.IsolatedAsyncioTestCase):
    def test_local_pdf_does_not_require_remote_configuration_or_transport(self):
        runner = Mock(return_value={"ok": True})
        with patch.object(providers, "remote_client", side_effect=AssertionError("remote accessed")):
            result = providers.parse_document("unused", "unused", runner, config={
                "mineru_provider": "local", "asr_provider": "remote_only", "base_url": "invalid",
            })
        self.assertEqual(result["provider"], "local")
        runner.assert_called_once()

    async def test_local_asr_keeps_original_result_and_never_contacts_node(self):
        manager = ModelManager(Path("/not-loaded"))
        manager._transcribe_local = AsyncMock(return_value=("local", "Chinese", .1))
        with patch.object(providers, "remote_client", side_effect=AssertionError("remote accessed")):
            result = await manager.transcribe([0.0] * 160, None, "", inference_config={
                "asr_provider": "local", "base_url": "invalid",
            })
        self.assertEqual(result, ("local", "Chinese", .1))

    def test_uncertain_remote_pdf_error_cannot_fall_back(self):
        runner = Mock()
        with patch.object(providers.RemoteMinerUProvider, "parse", side_effect=TimeoutError("uncertain")):
            with self.assertRaises(TimeoutError):
                providers.parse_document("unused", "unused", runner, config={
                    **providers.DEFAULTS, "mineru_provider": "remote_preferred",
                })
        runner.assert_not_called()

    def test_proven_unaccepted_pdf_can_fall_back_but_remote_only_cannot(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "receipt.json"
            atomic_json(path, {"state": "proven_not_accepted"})
            runner = Mock(return_value={})
            with patch.object(providers.RemoteMinerUProvider, "parse", side_effect=ProvenNotAccepted()):
                result = providers.parse_document("unused", "unused", runner, config={
                    **providers.DEFAULTS, "mineru_provider": "remote_preferred",
                }, receipt_path=path)
                self.assertTrue(result["fallback"])
                self.assertEqual(operations.receipt(path)["state"], "local_fallback")
                with self.assertRaises(ProvenNotAccepted):
                    providers.parse_document("unused", "unused", runner, config={
                        **providers.DEFAULTS, "mineru_provider": "remote_only",
                    }, receipt_path=path)
            runner.assert_called_once()

    async def test_uncertain_remote_asr_error_never_uses_local_model(self):
        manager = ModelManager(Path("/not-loaded"))
        manager.schedule_unload = Mock()
        manager._transcribe_local = AsyncMock()
        with patch.object(providers.RemoteASRProvider, "transcribe",
                          new=AsyncMock(side_effect=TimeoutError("uncertain"))):
            with self.assertRaises(TimeoutError):
                await manager.transcribe([0.0] * 160, None, "", inference_config={
                    **providers.DEFAULTS, "asr_provider": "remote_preferred",
                }, operation_dir=Path("/not-used"))
        manager._transcribe_local.assert_not_called()
        self.assertEqual(manager.active_jobs, 0)

    async def test_proven_nonacceptance_asr_fallback_records_receipt_and_metrics(self):
        manager = ModelManager(Path("/not-loaded"))
        manager.schedule_unload = Mock()
        manager._transcribe_local = AsyncMock(return_value=("local", "Chinese", .1))
        metrics = {}
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            atomic_json(folder / "receipt.json", {"state": "proven_not_accepted"})
            with patch.object(providers.RemoteASRProvider, "transcribe",
                              new=AsyncMock(side_effect=ProvenNotAccepted())):
                result = await manager.transcribe([0.0] * 160, None, "", inference_config={
                    **providers.DEFAULTS, "asr_provider": "remote_preferred",
                }, operation_dir=folder, metrics=metrics)
            self.assertEqual(operations.receipt(folder / "receipt.json")["state"], "local_fallback")
        self.assertEqual(result[:2], ("local", "Chinese"))
        self.assertTrue(metrics["fallback"])
        self.assertEqual(manager.active_jobs, 0)
        manager._transcribe_local.assert_awaited_once()

    async def test_asr_cancellation_never_triggers_local_fallback(self):
        manager = ModelManager(Path("/not-loaded"))
        manager.schedule_unload = Mock()
        manager._transcribe_local = AsyncMock()
        with patch.object(providers.RemoteASRProvider, "transcribe",
                          new=AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await manager.transcribe([0.0] * 160, None, "", inference_config={
                    **providers.DEFAULTS, "asr_provider": "remote_preferred",
                }, operation_dir=Path("/not-used"))
        manager._transcribe_local.assert_not_called()
        self.assertEqual(manager.active_jobs, 0)


if __name__ == "__main__":
    unittest.main()
