"""Boundary regression tests for user-supplied ASR and live parameters."""
from tests import isolation  # noqa: F401
import copy
import json
import unittest

from qwen_asr_app.settings import (
    DEFAULT_ASR_PARAMS,
    DEFAULT_LIVE_PARAMS,
    normalize_asr_params,
    normalize_live_params,
)

INTEGER_FIELDS = {
    "max_tokens": (16, 16_384),
    "batch_size": (1, 4),
    "top_k": (0, 500),
    "min_tokens_to_keep": (1, 100),
    "repetition_context_size": (0, 16_384),
    "prefill_step_size": (128, 16_384),
}
NUMBER_FIELDS = {
    "temperature": (0.0, 2.0),
    "top_p": (0.0, 1.0),
    "min_p": (0.0, 1.0),
    "chunk_duration": (1.0, 1_200.0),
    "min_chunk_duration": (0.1, 30.0),
}
LIVE_FIELDS = {
    "partial_interval_seconds": (2.0, 15.0),
    "silence_finalize_seconds": (0.5, 4.0),
    "max_segment_seconds": (15.0, 90.0),
    "min_voiced_seconds": (0.2, 2.0),
}


class ASRParameterValidation(unittest.TestCase):
    def test_integer_boundaries_and_compatible_representations(self):
        for name, (low, high) in INTEGER_FIELDS.items():
            for value in (low, high, str(low), str(high), float(low), float(high)):
                with self.subTest(name=name, value=value):
                    result = normalize_asr_params({name: value})[name]
                    self.assertEqual(result, int(value))
                    self.assertIs(type(result), int)

    def test_integer_range_limits(self):
        for name, (low, high) in INTEGER_FIELDS.items():
            for value in (low - 1, high + 1, -(10**1000), 10**1000):
                with self.subTest(name=name, value_type=type(value).__name__):
                    with self.assertRaises(ValueError):
                        normalize_asr_params({name: value})

    def test_fractional_integer_parameters_are_never_truncated(self):
        for name, (low, _) in INTEGER_FIELDS.items():
            for value in (low + 0.25, low + 0.999):
                with self.subTest(name=name, value=value):
                    with self.assertRaises(ValueError):
                        normalize_asr_params({name: value})

    def test_integer_nonfinite_and_invalid_types_are_validation_errors(self):
        for name in INTEGER_FIELDS:
            for value in (float("inf"), float("-inf"), float("nan"), None, [], {}, "2.5"):
                with self.subTest(name=name, value_type=type(value).__name__):
                    with self.assertRaises(ValueError):
                        normalize_asr_params({name: value})

    def test_booleans_are_not_numeric_asr_parameters(self):
        for name in (*INTEGER_FIELDS, *NUMBER_FIELDS, "repetition_penalty"):
            for value in (True, False):
                with self.subTest(name=name, value=value):
                    with self.assertRaises(ValueError):
                        normalize_asr_params({name: value})

    def test_number_boundaries_and_numeric_strings(self):
        for name, (low, high) in NUMBER_FIELDS.items():
            for value in (low, high, str(low), str(high)):
                with self.subTest(name=name, value=value):
                    result = normalize_asr_params({name: value})[name]
                    self.assertEqual(result, float(value))
                    self.assertIs(type(result), float)

    def test_invalid_numbers_never_escape_as_overflow_errors(self):
        for name in (*NUMBER_FIELDS, "repetition_penalty"):
            for value in (float("inf"), float("-inf"), float("nan"), 10**1000, [], {}):
                with self.subTest(name=name, value_type=type(value).__name__):
                    with self.assertRaises(ValueError):
                        normalize_asr_params({name: value})

    def test_number_range_limits(self):
        for name, (low, high) in NUMBER_FIELDS.items():
            for value in (low - 0.01, high + 0.01):
                with self.subTest(name=name, value=value):
                    with self.assertRaises(ValueError):
                        normalize_asr_params({name: value})

    def test_repetition_penalty_remains_optional(self):
        for value in (None, 0.1, 5, "1.2"):
            with self.subTest(value=value):
                self.assertEqual(
                    normalize_asr_params({"repetition_penalty": value})["repetition_penalty"],
                    None if value is None else float(value),
                )

    def test_fallback_token_limit_is_validated(self):
        for value in (True, 384.5, float("inf"), float("nan"), 0, 16_385):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    normalize_asr_params({}, value)
        self.assertEqual(normalize_asr_params({}, 512)["max_tokens"], 512)
        self.assertEqual(normalize_asr_params({"max_tokens": 1024}, 512)["max_tokens"], 1024)

    def test_json_input_defaults_and_no_mutation(self):
        raw = {"hotwords": ["  laser  ", "fiber"], "temperature": "0.2"}
        original, defaults = copy.deepcopy(raw), copy.deepcopy(DEFAULT_ASR_PARAMS)
        result = normalize_asr_params(json.dumps(raw))
        self.assertEqual(result["hotwords"], ["laser", "fiber"])
        normalize_asr_params(raw)["hotwords"].append("not-shared")
        self.assertEqual(raw, original)
        self.assertEqual(DEFAULT_ASR_PARAMS, defaults)
        self.assertEqual(normalize_asr_params(None), normalize_asr_params({}))

    def test_parameter_shape_unknown_fields_and_duration_order(self):
        for raw in ([], 1, True, "[]", {"unknown": 1},
                    {"chunk_duration": 1, "min_chunk_duration": 2},
                    {"verbose": "false"}):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    normalize_asr_params(raw)


class LiveParameterValidation(unittest.TestCase):
    def test_boundaries_and_numeric_strings(self):
        for name, (low, high) in LIVE_FIELDS.items():
            for value in (low, high, str(low), str(high)):
                with self.subTest(name=name, value=value):
                    self.assertEqual(normalize_live_params({name: value})[name], float(value))

    def test_range_limits(self):
        for name, (low, high) in LIVE_FIELDS.items():
            for value in (low - 0.01, high + 0.01):
                with self.subTest(name=name, value=value):
                    with self.assertRaises(ValueError):
                        normalize_live_params({name: value})

    def test_boolean_values_are_rejected(self):
        for name in LIVE_FIELDS:
            for value in (True, False):
                with self.subTest(name=name, value=value):
                    with self.assertRaises(ValueError):
                        normalize_live_params({name: value})

    def test_invalid_numbers_are_validation_errors(self):
        for name in LIVE_FIELDS:
            for value in (float("inf"), float("-inf"), float("nan"), 10**1000, None, [], {}):
                with self.subTest(name=name, value_type=type(value).__name__):
                    with self.assertRaises(ValueError):
                        normalize_live_params({name: value})

    def test_defaults_shape_and_no_mutation(self):
        defaults = copy.deepcopy(DEFAULT_LIVE_PARAMS)
        self.assertEqual(normalize_live_params(None), defaults)
        for raw in ([], True, 1, {"unknown": 2}):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    normalize_live_params(raw)
        raw = {"max_segment_seconds": 60}
        normalize_live_params(raw)
        self.assertEqual(raw, {"max_segment_seconds": 60})
        self.assertEqual(DEFAULT_LIVE_PARAMS, defaults)


if __name__ == "__main__":
    unittest.main()
