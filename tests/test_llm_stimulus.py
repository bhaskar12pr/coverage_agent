import unittest
from unittest.mock import MagicMock, patch

try:
    import anthropic  # noqa: F401
    import pydantic  # noqa: F401

    _HAS_LLM_DEPS = True
except ImportError:
    _HAS_LLM_DEPS = False


@unittest.skipUnless(_HAS_LLM_DEPS, "anthropic/pydantic not installed — suggest-stimulus is an optional extra")
class TestSuggestStimulus(unittest.TestCase):
    def test_feasible_suggestion_is_returned_per_signal(self):
        from coverage_agent.exclude.candidates import ExcludeCandidate, UNEXPLAINED_GAP
        from coverage_agent.llm.stimulus import BatchStimulusSuggestions, StimulusSuggestion, suggest_stimulus

        candidates = [
            ExcludeCandidate(instance="i", signal="gpio_out[3]", disposition=UNEXPLAINED_GAP,
                              reason="no justification", source="", gap_kind="never_toggled"),
        ]
        fake_response = MagicMock()
        fake_response.parsed_output = BatchStimulusSuggestions(suggestions=[
            StimulusSuggestion(
                signal="gpio_out[3]",
                feasible=True,
                confidence="high",
                steps=["Write GPIO_DIR reg (addr 0x0) with bit 3 set", "Write GPIO_OUT reg (addr 0x4) with bit 3 = 1, then 0"],
                rationale="gpio_out[3] only changes via a write to the GPIO_OUT register at case paddr[7:2]==6'h1",
            ),
        ])

        with patch("anthropic.Anthropic") as mock_client_cls:
            mock_client_cls.return_value.messages.parse.return_value = fake_response
            result = suggest_stimulus(candidates, {"f.v": "module m; endmodule"}, {"NUM_GPIO": 8})

        self.assertIn("i::gpio_out[3]", result)
        s = result["i::gpio_out[3]"]
        self.assertTrue(s.feasible)
        self.assertEqual(len(s.steps), 2)

    def test_infeasible_signal_is_reported_not_fabricated(self):
        from coverage_agent.exclude.candidates import ExcludeCandidate, UNEXPLAINED_GAP
        from coverage_agent.llm.stimulus import BatchStimulusSuggestions, StimulusSuggestion, suggest_stimulus

        candidates = [
            ExcludeCandidate(instance="i", signal="mystery_sig", disposition=UNEXPLAINED_GAP,
                              reason="no justification", source="", gap_kind="never_toggled"),
        ]
        fake_response = MagicMock()
        fake_response.parsed_output = BatchStimulusSuggestions(suggestions=[
            StimulusSuggestion(
                signal="mystery_sig", feasible=False, confidence="low", steps=[],
                rationale="no driver for this signal is visible in the given RTL",
            ),
        ])
        with patch("anthropic.Anthropic") as mock_client_cls:
            mock_client_cls.return_value.messages.parse.return_value = fake_response
            result = suggest_stimulus(candidates, {"f.v": "module m; endmodule"}, {})

        self.assertFalse(result["i::mystery_sig"].feasible)
        self.assertEqual(result["i::mystery_sig"].steps, [])

    def test_missing_direction_gap_kind_is_described_correctly(self):
        from coverage_agent.exclude.candidates import ExcludeCandidate, UNEXPLAINED_GAP
        from coverage_agent.llm.stimulus import _describe_gap

        c1 = ExcludeCandidate(instance="i", signal="s", disposition=UNEXPLAINED_GAP,
                               reason="", source="", gap_kind="missing_0_to_1")
        c2 = ExcludeCandidate(instance="i", signal="s", disposition=UNEXPLAINED_GAP,
                               reason="", source="", gap_kind="missing_1_to_0")
        self.assertIn("never 0->1", _describe_gap(c1))
        self.assertIn("never 1->0", _describe_gap(c2))


if __name__ == "__main__":
    unittest.main()
