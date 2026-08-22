"""Optional LLM-assisted judgment for exclude candidates the RTL
scanner found ZERO evidence for.

This is not a replacement for coverage_agent/rtl/scanner.py — it is a
strictly lower-trust fallback, only ever invoked on candidates with
`llm_eligible=True` (see coverage_agent/exclude/candidates.py): a
partial-toggle gap, or a gap the scanner already resolved via a live
generate branch, is NEVER sent here, because those are already either
structurally impossible to be a tie-off or already deterministically
explained.

An LLM judgment is a probabilistic opinion, not a pattern match. It is
tagged with a distinct disposition (LLM_SUGGESTED_EXCLUDE) that
coverage_agent/exclude/formatter.py holds to the SAME bar as a manual
override: approved=true is not enough, a human must also write a
"note". Opt-in only (--llm on suggest-excludes); requires
`pip install anthropic pydantic` and API credentials (see README.md)
— never imported unless requested.
"""

from typing import Literal

from pydantic import BaseModel

from coverage_agent.exclude.candidates import ExcludeCandidate, LLM_SUGGESTED_EXCLUDE, UNEXPLAINED_GAP

DEFAULT_MODEL = "claude-opus-5"

_SYSTEM_PROMPT = """You are assisting a SoC verification engineer reviewing VCS toggle \
coverage gaps for one IP instance. For each signal listed, you are given the FULL RTL \
source of the module(s) involved and the specific parameter values active for this \
derivative/config.

Your job: decide, from the RTL alone, whether the signal is STRUCTURALLY GUARANTEED \
never to toggle under these exact parameter values (dead_by_design — e.g. tied to a \
constant through a path a simple pattern scanner would miss, or gated off by a \
condition it wouldn't recognize) or whether the RTL shows it CAN toggle, meaning the \
coverage gap reflects a real, untested scenario (real_gap).

Be conservative. A wrong "dead_by_design" verdict hides a real verification gap from \
a human reviewer, so err toward real_gap or uncertain whenever the RTL is ambiguous, \
depends on values you can't fully trace, or depends on testbench/environment behavior \
you can't see from RTL alone. Always cite the specific RTL lines/expressions that \
ground your verdict in rtl_evidence — a verdict with no concrete RTL citation should \
be "uncertain"."""


class SignalJudgment(BaseModel):
    signal: str
    verdict: Literal["dead_by_design", "real_gap", "uncertain"]
    confidence: Literal["low", "medium", "high"]
    reasoning: str
    rtl_evidence: str


class BatchJudgment(BaseModel):
    judgments: list[SignalJudgment]


def _build_prompt(instance: str, params: dict, rtl_text: str, candidates: list[ExcludeCandidate]) -> str:
    signal_list = "\n".join(f"- {c.signal}" for c in candidates)
    params_text = ", ".join(f"{k}={v}" for k, v in sorted(params.items())) or "(none)"
    return f"""Instance: {instance}
Active parameter values for this derivative: {params_text}

RTL source:
```verilog
{rtl_text}
```

These signals have a "never toggled" coverage gap that a regex-based tie-off/generate-gate \
scanner could NOT classify (no matching pattern found):
{signal_list}

Return a judgment for every signal listed above — one entry per signal, using the exact \
signal name given."""


def judge_candidates(
    candidates: list[ExcludeCandidate],
    rtl_texts: dict[str, str],
    params: dict,
    model: str = DEFAULT_MODEL,
) -> list[ExcludeCandidate]:
    """Ask Claude to judge the llm_eligible candidates. Returns a NEW
    list — candidates with llm_eligible=False pass through untouched;
    eligible ones get their disposition/reason replaced with the LLM's
    verdict (still gated by formatter.py's note requirement before
    anything reaches a real exclusion file)."""
    import anthropic

    eligible = [c for c in candidates if c.llm_eligible]
    if not eligible:
        return candidates

    client = anthropic.Anthropic()
    by_instance: dict[str, list[ExcludeCandidate]] = {}
    for c in eligible:
        by_instance.setdefault(c.instance, []).append(c)

    combined_rtl = "\n\n".join(f"// --- {path} ---\n{text}" for path, text in rtl_texts.items())

    judged_by_key: dict[tuple[str, str], SignalJudgment] = {}
    for instance, insts in by_instance.items():
        prompt = _build_prompt(instance, params, combined_rtl, insts)
        response = client.messages.parse(
            model=model,
            max_tokens=16000,
            system=_SYSTEM_PROMPT,
            thinking={"type": "adaptive"},
            messages=[{"role": "user", "content": prompt}],
            output_format=BatchJudgment,
        )
        for j in response.parsed_output.judgments:
            judged_by_key[(instance, j.signal)] = j

    updated: list[ExcludeCandidate] = []
    for c in candidates:
        if not c.llm_eligible:
            updated.append(c)
            continue
        j = judged_by_key.get((c.instance, c.signal))
        if j is None:
            updated.append(c)  # model didn't return a judgment for this signal — leave as unexplained
            continue
        if j.verdict == "dead_by_design" and j.confidence in ("medium", "high"):
            updated.append(
                ExcludeCandidate(
                    instance=c.instance,
                    signal=c.signal,
                    disposition=LLM_SUGGESTED_EXCLUDE,
                    reason=f"[LLM, confidence={j.confidence}] {j.reasoning} Evidence: {j.rtl_evidence}",
                    source="llm-review (not a deterministic RTL match)",
                )
            )
        else:
            updated.append(
                ExcludeCandidate(
                    instance=c.instance,
                    signal=c.signal,
                    disposition=UNEXPLAINED_GAP,
                    reason=f"[LLM, verdict={j.verdict}, confidence={j.confidence}] {j.reasoning}",
                    source="",
                )
            )
    return updated
