import unittest
from unittest.mock import MagicMock, patch

from coverage_agent.exclude.candidates import LLM_SUGGESTED_EXCLUDE
from coverage_agent.exclude.formatter import format_ucm_exclusions

try:
    import anthropic  # noqa: F401
    import pydantic  # noqa: F401

    _HAS_LLM_DEPS = True
except ImportError:
    _HAS_LLM_DEPS = False


class TestFormatterLlmNoteRequirement(unittest.TestCase):
    """Doesn't need anthropic/pydantic — exercises the trust boundary
    that matters regardless of whether the LLM feature is installed:
    an llm_suggested_exclude is held to the same bar as a manual
    override, never the lighter bar an RTL match gets."""

    def test_llm_suggestion_without_note_is_skipped(self):
        entries = [
            {"id": "i::foo", "instance": "i", "signal": "foo", "disposition": LLM_SUGGESTED_EXCLUDE,
             "reason": "LLM says dead", "source": "llm-review", "approved": True, "note": None},
        ]
        text, warnings = format_ucm_exclusions(entries)
        self.assertNotIn('"foo"', text)
        self.assertEqual(len(warnings), 1)

    def test_llm_suggestion_with_note_is_included_and_tagged(self):
        entries = [
            {"id": "i::foo", "instance": "i", "signal": "foo", "disposition": LLM_SUGGESTED_EXCLUDE,
             "reason": "LLM says dead", "source": "llm-review", "approved": True,
             "note": "reviewed, agree with LLM reasoning"},
        ]
        text, warnings = format_ucm_exclusions(entries)
        self.assertIn('"foo"', text)
        self.assertIn("LLM SUGGESTION, HUMAN-CONFIRMED", text)
        self.assertEqual(warnings, [])


@unittest.skipUnless(_HAS_LLM_DEPS, "anthropic/pydantic not installed — --llm is an optional extra")
class TestJudgeCandidates(unittest.TestCase):
    def test_high_confidence_dead_by_design_becomes_llm_suggested_exclude(self):
        from coverage_agent.exclude.candidates import ExcludeCandidate, UNEXPLAINED_GAP
        from coverage_agent.llm.judge import BatchJudgment, SignalJudgment, judge_candidates

        candidates = [
            ExcludeCandidate(instance="i", signal="foo", disposition=UNEXPLAINED_GAP,
                              reason="no justification", source="", llm_eligible=True),
            ExcludeCandidate(instance="i", signal="bar", disposition=UNEXPLAINED_GAP,
                              reason="partial gap, structurally can't be a tie-off",
                              source="", llm_eligible=False),
        ]
        fake_response = MagicMock()
        fake_response.parsed_output = BatchJudgment(judgments=[
            SignalJudgment(signal="foo", verdict="dead_by_design", confidence="high",
                            reasoning="tied via a case default", rtl_evidence="default: foo = 1'b0;"),
        ])

        with patch("anthropic.Anthropic") as mock_client_cls:
            mock_client_cls.return_value.messages.parse.return_value = fake_response
            result = judge_candidates(candidates, {"f.v": "module m; endmodule"}, {"P": 1})

        by_signal = {c.signal: c for c in result}
        self.assertEqual(by_signal["foo"].disposition, LLM_SUGGESTED_EXCLUDE)
        self.assertIn("tied via a case default", by_signal["foo"].reason)
        # llm_eligible=False candidates must never be sent to the model or altered
        self.assertIs(by_signal["bar"], candidates[1])
        mock_client_cls.return_value.messages.parse.assert_called_once()

    def test_low_confidence_stays_unexplained_not_excluded(self):
        from coverage_agent.exclude.candidates import ExcludeCandidate, UNEXPLAINED_GAP
        from coverage_agent.llm.judge import BatchJudgment, SignalJudgment, judge_candidates

        candidates = [
            ExcludeCandidate(instance="i", signal="foo", disposition=UNEXPLAINED_GAP,
                              reason="no justification", source="", llm_eligible=True),
        ]
        fake_response = MagicMock()
        fake_response.parsed_output = BatchJudgment(judgments=[
            SignalJudgment(signal="foo", verdict="dead_by_design", confidence="low",
                            reasoning="maybe tied, not fully sure", rtl_evidence="ambiguous"),
        ])
        with patch("anthropic.Anthropic") as mock_client_cls:
            mock_client_cls.return_value.messages.parse.return_value = fake_response
            result = judge_candidates(candidates, {"f.v": "module m; endmodule"}, {})

        self.assertEqual(result[0].disposition, UNEXPLAINED_GAP)

    def test_real_gap_verdict_stays_unexplained(self):
        from coverage_agent.exclude.candidates import ExcludeCandidate, UNEXPLAINED_GAP
        from coverage_agent.llm.judge import BatchJudgment, SignalJudgment, judge_candidates

        candidates = [
            ExcludeCandidate(instance="i", signal="foo", disposition=UNEXPLAINED_GAP,
                              reason="no justification", source="", llm_eligible=True),
        ]
        fake_response = MagicMock()
        fake_response.parsed_output = BatchJudgment(judgments=[
            SignalJudgment(signal="foo", verdict="real_gap", confidence="high",
                            reasoning="driven by live combinational logic", rtl_evidence="assign foo = a & b;"),
        ])
        with patch("anthropic.Anthropic") as mock_client_cls:
            mock_client_cls.return_value.messages.parse.return_value = fake_response
            result = judge_candidates(candidates, {"f.v": "module m; endmodule"}, {})

        self.assertEqual(result[0].disposition, UNEXPLAINED_GAP)
        self.assertIn("live combinational logic", result[0].reason)


if __name__ == "__main__":
    unittest.main()
