"""Optional LLM-assisted stimulus suggestions for real toggle coverage
gaps — the "close the loop" step after suggest-excludes has already
filtered out anything RTL-justified as dead.

Lower stakes than judge.py's exclude suggestions: a bad stimulus
suggestion just wastes an engineer's time trying it, it can't silently
hide a bug the way a wrong exclusion can. So this has no
approval/note gate — it's advisory text for a human to act on, not
something that changes what gets excluded from coverage. Still opt-in
(--llm on suggest-stimulus) and still requires `pip install anthropic
pydantic` + credentials — never imported unless requested.
"""

from typing import Literal

from pydantic import BaseModel

from coverage_agent.exclude.candidates import ExcludeCandidate

DEFAULT_MODEL = "claude-opus-5"

_SYSTEM_PROMPT = """You are assisting a SoC verification engineer who needs to close toggle \
coverage gaps for one IP instance. For each signal listed, you are given the FULL RTL source \
of the module(s) involved, the active parameter values for this derivative/config, and which \
toggle transition(s) are missing (never toggled at all, or only one direction).

Your job: propose CONCRETE stimulus — in terms of the module's actual interface (e.g. specific \
register writes/addresses if you can identify them from a register decode in the RTL, specific \
port drives, reset/clock sequencing) — that would exercise the missing transition(s). Ground \
every suggestion in the RTL you were given: name the actual signal/register/case-branch that \
needs to be hit, not generic advice like "write more test cases".

If you cannot determine a way to toggle the signal from the RTL alone (e.g. it depends on \
values/timing you can't see, or you increasingly suspect it may actually be dead despite not \
matching a known tie-off/gate pattern), say so explicitly with confidence="low" and explain why \
— don't invent a plausible-sounding but ungrounded sequence. That "I think this might actually be \
dead" signal is itself useful to the reviewer."""


class StimulusSuggestion(BaseModel):
    signal: str
    feasible: bool  # false = "I don't think RTL alone gives you a way to hit this"
    confidence: Literal["low", "medium", "high"]
    steps: list[str]  # ordered, concrete stimulus steps (register writes, port drives, sequencing)
    rationale: str  # why these steps should produce the missing transition, citing RTL


class BatchStimulusSuggestions(BaseModel):
    suggestions: list[StimulusSuggestion]


def _describe_gap(c: ExcludeCandidate) -> str:
    return {
        "never_toggled": "never toggled at all (neither 0->1 nor 1->0 seen)",
        "missing_0_to_1": "toggled 1->0 but never 0->1",
        "missing_1_to_0": "toggled 0->1 but never 1->0",
    }.get(c.gap_kind, "never toggled at all")


def _build_prompt(instance: str, params: dict, rtl_text: str, candidates: list[ExcludeCandidate]) -> str:
    gap_list = "\n".join(f"- {c.signal}: {_describe_gap(c)}" for c in candidates)
    params_text = ", ".join(f"{k}={v}" for k, v in sorted(params.items())) or "(none)"
    return f"""Instance: {instance}
Active parameter values for this derivative: {params_text}

RTL source:
```verilog
{rtl_text}
```

These signals have a real, unexplained toggle coverage gap (not RTL-justified as dead):
{gap_list}

Propose stimulus for every signal listed above — one entry per signal, using the exact \
signal name given."""


def suggest_stimulus(
    candidates: list[ExcludeCandidate],
    rtl_texts: dict[str, str],
    params: dict,
    model: str = DEFAULT_MODEL,
) -> dict[str, StimulusSuggestion]:
    """Ask Claude for stimulus ideas for real gaps (UNEXPLAINED_GAP
    candidates only — callers are responsible for filtering; see
    cli.py). Returns {candidate_id: StimulusSuggestion}, one entry per
    candidate that got a response back."""
    import anthropic

    if not candidates:
        return {}

    client = anthropic.Anthropic()
    by_instance: dict[str, list[ExcludeCandidate]] = {}
    for c in candidates:
        by_instance.setdefault(c.instance, []).append(c)

    combined_rtl = "\n\n".join(f"// --- {path} ---\n{text}" for path, text in rtl_texts.items())

    results: dict[str, StimulusSuggestion] = {}
    for instance, insts in by_instance.items():
        prompt = _build_prompt(instance, params, combined_rtl, insts)
        response = client.messages.parse(
            model=model,
            max_tokens=16000,
            system=_SYSTEM_PROMPT,
            thinking={"type": "adaptive"},
            messages=[{"role": "user", "content": prompt}],
            output_format=BatchStimulusSuggestions,
        )
        by_signal = {s.signal: s for s in response.parsed_output.suggestions}
        for c in insts:
            if c.signal in by_signal:
                results[c.candidate_id] = by_signal[c.signal]

    return results


def format_stimulus_console(candidates: list[ExcludeCandidate], suggestions: dict[str, StimulusSuggestion]) -> str:
    lines = [f"Stimulus suggestions for {len(candidates)} real gap(s):"]
    for c in candidates:
        s = suggestions.get(c.candidate_id)
        lines.append(f"\n{c.instance}.{c.signal} ({_describe_gap(c)})")
        if s is None:
            lines.append("  (no suggestion returned)")
            continue
        if not s.feasible:
            lines.append(f"  [feasible=false, confidence={s.confidence}] {s.rationale}")
            continue
        lines.append(f"  [confidence={s.confidence}]")
        for i, step in enumerate(s.steps, 1):
            lines.append(f"    {i}. {step}")
        lines.append(f"  Rationale: {s.rationale}")
    return "\n".join(lines)
