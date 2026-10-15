"""Protect scientific values while preserving bounded classroom context."""
from tests import isolation  # noqa: F401
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from qwen_asr_app.context_budget import (
    DEFAULT_CONTEXT_WINDOW, chunk_text_blocks, estimate_tokens,
    fit_previous_context, normalize_context_window, truncate_tail_to_tokens,
)
from qwen_asr_app.terminology import (
    ai_terminology_context, asr_params_with_terminology,
    asr_terminology_context, normalize_terminology, terminology_stats,
)
from qwen_asr_app.transcription_assist import service
from qwen_asr_app.transcription_assist.validation import (
    correction_risks, protect_correction, sensitive_signature,
)


class ScientificCorrectionTests(unittest.TestCase):
    def test_leading_dot_value_changes_are_high_risk(self):
        for before, after in (
            ("NA = .22", "NA = .20"),
            ("P = .22 mW", "P = .22 W"),
            ("E = -.22", "E = +.22"),
            ("I = .22e-3 A", "I = .22e-2 A"),
            ("eta = .22%", "eta = .20%"),
            ("E = \u2212.22", "E = .22"),
        ):
            with self.subTest(before=before, after=after):
                self.assertIn("数字或单位变化", correction_risks(before, after))

    def test_leading_dot_candidate_is_retained_for_review_not_applied(self):
        segment = {"id": 1, "raw": "NA = .22"}
        evidence = [{"text": "NA = .20", "citation_id": "K1"}]
        safe, candidate = protect_correction(segment, "NA = .20", evidence)
        self.assertEqual(safe, segment["raw"])
        self.assertEqual(candidate["after"], "NA = .20")
        self.assertEqual(candidate["status"], "待核对")
        candidate["evidence"][0]["text"] = "not-shared"
        self.assertEqual(evidence[0]["text"], "NA = .20")

    def test_unchanged_values_allow_punctuation_edits(self):
        self.assertEqual(correction_risks("P = .22 mW，NA = .20。", "P = .22 mW；NA = .20。"), [])

    def test_leading_dot_literal_is_matched_once_in_full(self):
        signature = sensitive_signature("NA=.22")
        self.assertEqual(dict(signature["numbers_units"]), {".22": 1})
        signature = sensitive_signature("P = -.22e-3 mW")
        self.assertEqual(dict(signature["numbers_units"]), {"-.22e-3mW": 1})

    def test_timestamps_and_unit_spacing_do_not_create_numeric_changes(self):
        self.assertEqual(correction_risks("[00:30] P=.22\u00b5W", "[00:45] P=.22 \u03bcW"), [])

    def test_existing_integer_decimal_and_scientific_values_still_protected(self):
        for before, after in (("P=22", "P=20"), ("NA=0.22", "NA=0.20"),
                              ("I=2e-3 A", "I=2e-2 A"), ("P=1,000 mW", "P=100 mW")):
            with self.subTest(before=before):
                self.assertTrue(correction_risks(before, after))


class ContextBudgetTests(unittest.TestCase):
    def test_context_boundaries_and_numeric_strings(self):
        for value in (2048, 2_000_000, "4096", 4096.0):
            with self.subTest(value=value):
                self.assertEqual(normalize_context_window(value), int(value))
        for value in (None, "", 0, 0.0):
            with self.subTest(default_sentinel=value):
                self.assertEqual(normalize_context_window(value), DEFAULT_CONTEXT_WINDOW)

    def test_context_invalid_types_fractions_and_nonfinite_values(self):
        for value in (False, True, 4096.5, float("inf"), float("-inf"), float("nan"),
                      [], {}, "4096.5", 2047, 2_000_001, 10**1000):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(ValueError):
                    normalize_context_window(value)

    def test_truncation_keeps_a_suffix_within_the_estimated_budget(self):
        text = "old history 旧记录 " * 30 + "最新内容"
        for budget in range(65):
            with self.subTest(budget=budget):
                tail = truncate_tail_to_tokens(text, budget)
                self.assertTrue(text.endswith(tail))
                self.assertLessEqual(estimate_tokens(tail), budget)
                if budget >= estimate_tokens("最新内容"):
                    self.assertTrue(tail.endswith("最新内容"))

    def test_long_block_splits_without_losing_any_source_character(self):
        block = "光学 optical $E=h\\nu$ " * 100
        chunks = chunk_text_blocks([block], 2048, max_source_tokens=128)
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks), block.strip())
        self.assertTrue(all(0 < estimate_tokens(chunk) <= 128 for chunk in chunks))

    def test_complete_blocks_keep_order(self):
        blocks = ["first 第一段", "second 第二段", "third 第三段"]
        self.assertEqual(chunk_text_blocks(blocks, 4096), ["\n\n".join(blocks)])

    def test_fit_previous_context_reserves_output_and_rejects_oversized_current_text(self):
        history, output = fit_previous_context("新转录" * 100, "旧记录" * 1000, "instructions", 2048, 512)
        self.assertTrue(("旧记录" * 1000).endswith(history))
        self.assertLessEqual(estimate_tokens(history) + estimate_tokens("新转录" * 100)
            + estimate_tokens("instructions") + 320 + output, 2048)
        with self.assertRaises(ValueError):
            fit_previous_context("过长转录" * 1000, "", "", 2048, 512)


class TerminologyContractTests(unittest.TestCase):
    def test_normalization_deduplicates_without_mutating_input(self):
        values = [" laser ", "LASER", "", "光场限制因子"]
        saved = copy.deepcopy(values)
        self.assertEqual(normalize_terminology(values), ["laser", "光场限制因子"])
        self.assertEqual(values, saved)

    def test_oversized_library_or_entry_is_not_silently_truncated(self):
        for values in (["x" * 121], [f"term-{n}" for n in range(501)]):
            with self.subTest(size=len(values)):
                with self.assertRaises(ValueError):
                    normalize_terminology(values)

    def test_hotwords_preserve_existing_parameters_and_respect_the_cap(self):
        params = {"temperature": 0.2, "hotwords": ["laser"]}
        result = asr_params_with_terminology(params, ["LASER"] + [f"term-{n}" for n in range(200)])
        self.assertEqual(result["temperature"], 0.2)
        self.assertEqual(len(result["hotwords"]), 100)
        self.assertEqual(result["hotwords"][0], "laser")
        self.assertEqual(params, {"temperature": 0.2, "hotwords": ["laser"]})

    def test_context_prefers_nearby_terms_and_keeps_manual_context(self):
        values = ["wave", "laser", "fiber"]
        self.assertIn("manual", asr_terminology_context("manual", values))
        context = ai_terminology_context(values, context_window=2048, source_text="fiber")
        self.assertLess(context.index("- fiber"), context.index("- wave"))
        self.assertEqual(terminology_stats(values)["stored_terms"], 3)


class EvidenceConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def test_evidence_filters_stale_material_and_bounds_the_query(self):
        search = Mock(return_value=[
            {"text": "laser", "citation_id": "K1", "stale": True},
            {"text": "fiber", "citation_id": "K2", "origin": "original"},
        ])
        knowledge = SimpleNamespace(search=search, validate_evidence=lambda value: value)
        record = SimpleNamespace(metadata={"category": "optics"})
        config = {"terminology": ["laser", "fiber"]}
        with patch.object(service, "_knowledge", knowledge):
            result = await service.evidence_config(record, config, "x" * 3000 + "fiber")
        self.assertEqual(len(search.call_args.args[1]), 2000)
        self.assertFalse(search.call_args.kwargs["include_derived"])
        self.assertEqual([e["citation_id"] for e in result["_knowledge_evidence"]], ["K2"])
        self.assertEqual(result["terminology"][0], "fiber")
        self.assertEqual(config, {"terminology": ["laser", "fiber"]})

    async def test_optional_retrieval_failure_preserves_the_config(self):
        knowledge = SimpleNamespace(search=Mock(side_effect=RuntimeError("offline")))
        config = {"terminology": ["laser"], "temperature": 0.0}
        with patch.object(service, "_knowledge", knowledge):
            result = await service.evidence_config(SimpleNamespace(metadata={"category": "optics"}), config, "laser")
        self.assertEqual(result["terminology"], ["laser"])
        self.assertIn("_knowledge_warning", result)
        self.assertEqual(config, {"terminology": ["laser"], "temperature": 0.0})

    async def test_disabled_knowledge_returns_an_independent_copy(self):
        config = {"terminology": ["laser"]}
        with patch.object(service, "_knowledge", None):
            result = await service.evidence_config(None, config, "laser")
        result["terminology"].append("fiber")
        self.assertEqual(config, {"terminology": ["laser"]})


if __name__ == "__main__":
    unittest.main()
