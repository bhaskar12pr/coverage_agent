"""CLI entry point: analyze VCS urg toggle coverage reports for gaps,
and suggest/generate toggle coverage exclusions."""

import argparse
import glob
import json
import os
import sys

from coverage_agent.exclude import (
    format_candidates_console,
    format_ucm_exclusions,
    format_verification_console,
    load_review_file,
    parse_ucm_exclusion_file,
    suggest_excludes,
    verify_exclusions,
    write_review_file,
)
from coverage_agent.exclude.verify import SUSPICIOUS, UNKNOWN_SIGNAL
from coverage_agent.model import ToggleBin
from coverage_agent.parsers import parse_urg_html, parse_urg_text
from coverage_agent.report import build_gap_report, format_console, to_csv, to_json
from coverage_agent.rtl.scanner import scan_rtl_files

_HTML_EXTS = (".html", ".htm")
_TEXT_EXTS = (".txt", ".rpt", ".log")


def _iter_input_files(path: str) -> list[str]:
    if os.path.isfile(path):
        return [path]
    if os.path.isdir(path):
        files = []
        for ext in _HTML_EXTS + _TEXT_EXTS:
            files.extend(glob.glob(os.path.join(path, "**", f"*{ext}"), recursive=True))
        return sorted(files)
    raise FileNotFoundError(path)


def _parse_file(path: str) -> list[ToggleBin]:
    ext = os.path.splitext(path)[1].lower()
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    if ext in _HTML_EXTS:
        return parse_urg_html(text)
    return parse_urg_text(text)


def cmd_analyze(args: argparse.Namespace) -> int:
    try:
        files = _iter_input_files(args.path)
    except FileNotFoundError:
        print(f"error: path not found: {args.path}", file=sys.stderr)
        return 2

    if not files:
        print(f"error: no .html/.txt report files found under {args.path}", file=sys.stderr)
        return 2

    all_bins: list[ToggleBin] = []
    for f in files:
        all_bins.extend(_parse_file(f))

    if not all_bins:
        print(
            f"warning: parsed {len(files)} file(s) but found 0 toggle bins — "
            "the report format may not match this parser's assumptions "
            "(see README.md troubleshooting).",
            file=sys.stderr,
        )

    report = build_gap_report(all_bins, scope=args.scope or "")

    print(format_console(report, top=args.top))

    if args.json:
        with open(args.json, "w") as f:
            f.write(to_json(report))
        print(f"\nWrote JSON: {args.json}")

    if args.csv:
        with open(args.csv, "w", newline="") as f:
            f.write(to_csv(report))
        print(f"Wrote CSV: {args.csv}")

    return 1 if report.total > 0 and report.gap_count > 0 else 0


def _load_config(config_path: str) -> dict:
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)


def _resolve_rtl_paths(args: argparse.Namespace, config: dict, config_path: str) -> list[str]:
    if args.rtl:
        rtl_paths = []
        for p in args.rtl:
            if os.path.isdir(p):
                rtl_paths.extend(sorted(glob.glob(os.path.join(p, "**", "*.v"), recursive=True)))
                rtl_paths.extend(sorted(glob.glob(os.path.join(p, "**", "*.sv"), recursive=True)))
            else:
                rtl_paths.append(p)
        return rtl_paths

    rtl_file = config.get("rtl_file")
    if not rtl_file:
        return []
    # Convention: rtl_file in a config is relative to that config's
    # project root, i.e. <project_root>/configs/<config>.json ->
    # rtl_file resolved against <project_root>/. See README.md.
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(config_path)))
    candidate = os.path.join(project_root, rtl_file)
    return [candidate] if os.path.isfile(candidate) else []


def _prepare_gap_analysis(args: argparse.Namespace):
    """Shared setup for suggest-excludes and suggest-stimulus: parse
    the toggle report, load the config, resolve+scan RTL. Returns
    (all_bins, config, rtl_paths, rtl_facts, merged_params, error_code)
    — error_code is None on success (an error is already printed to
    stderr by this point on failure, so the caller only checks the
    code)."""
    try:
        files = _iter_input_files(args.report)
    except FileNotFoundError:
        print(f"error: report path not found: {args.report}", file=sys.stderr)
        return None, None, None, None, None, 2
    if not files:
        print(f"error: no .html/.txt report files found under {args.report}", file=sys.stderr)
        return None, None, None, None, None, 2

    all_bins: list[ToggleBin] = []
    for f in files:
        all_bins.extend(_parse_file(f))

    try:
        config = _load_config(args.config)
    except (OSError, json.JSONDecodeError) as e:
        print(f"error: could not read config {args.config}: {e}", file=sys.stderr)
        return None, None, None, None, None, 2

    rtl_paths = _resolve_rtl_paths(args, config, args.config)
    if not rtl_paths:
        print(
            "error: no RTL files found. Pass --rtl <file-or-dir>, or set "
            "\"rtl_file\" in the config (resolved relative to the config's project root).",
            file=sys.stderr,
        )
        return None, None, None, None, None, 2
    missing = [p for p in rtl_paths if not os.path.isfile(p)]
    if missing:
        print(f"error: RTL file(s) not found: {missing}", file=sys.stderr)
        return None, None, None, None, None, 2

    rtl_facts = scan_rtl_files(rtl_paths)
    merged_params = {**rtl_facts.params, **config.get("params", {})}
    return all_bins, config, rtl_paths, rtl_facts, merged_params, None


def _read_rtl_texts(rtl_paths: list[str]) -> dict[str, str]:
    rtl_texts = {}
    for p in rtl_paths:
        with open(p, encoding="utf-8", errors="replace") as f:
            rtl_texts[p] = f.read()
    return rtl_texts


def cmd_suggest_excludes(args: argparse.Namespace) -> int:
    all_bins, config, rtl_paths, rtl_facts, merged_params, err = _prepare_gap_analysis(args)
    if err is not None:
        return err

    candidates = suggest_excludes(all_bins, rtl_facts, merged_params, scope=args.scope or "")

    if args.llm:
        n_eligible = sum(1 for c in candidates if c.llm_eligible)
        if n_eligible == 0:
            print("--llm requested but no candidates are eligible (RTL scanner already resolved everything).")
        else:
            try:
                from coverage_agent.llm import judge_candidates
            except ImportError as e:
                print(
                    f"error: --llm requires the 'anthropic' and 'pydantic' packages "
                    f"(pip install anthropic pydantic): {e}",
                    file=sys.stderr,
                )
                return 2
            print(f"Asking {args.llm_model} to judge {n_eligible} otherwise-unexplained gap(s)...")
            rtl_texts = _read_rtl_texts(rtl_paths)
            try:
                candidates = judge_candidates(candidates, rtl_texts, merged_params, model=args.llm_model)
            except Exception as e:  # noqa: BLE001 — surface any SDK/auth/API error, don't crash the run
                print(f"error: LLM judging failed, continuing with RTL-only results: {e}", file=sys.stderr)

    project = config.get("project", os.path.basename(args.config))
    ip_version = config.get("ip_version", "unknown")

    print(format_candidates_console(candidates, project, ip_version))

    write_review_file(args.out, candidates, project, ip_version)
    print(f"\nWrote review file: {args.out}")
    print("Nothing is excluded yet — edit that file, set \"approved\": true on entries you agree with, "
          "then run 'gen-excludes' on it.")

    return 0


def cmd_gen_excludes(args: argparse.Namespace) -> int:
    try:
        entries = load_review_file(args.review_file)
    except (OSError, json.JSONDecodeError) as e:
        print(f"error: could not read review file {args.review_file}: {e}", file=sys.stderr)
        return 2

    approved = [e for e in entries if e.get("approved") is True]
    if not approved:
        print("No entries with \"approved\": true found — nothing to generate.", file=sys.stderr)
        print("Edit the review file and set \"approved\": true on entries you've signed off on.", file=sys.stderr)
        return 1

    text, warnings = format_ucm_exclusions(entries)
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)

    with open(args.out, "w") as f:
        f.write(text)

    emitted = len(approved) - len(warnings)
    print(f"Wrote {args.out} ({emitted} exclusion(s); FORMAT UNVERIFIED — validate before real use, see README.md)")
    return 0


def cmd_suggest_stimulus(args: argparse.Namespace) -> int:
    all_bins, config, rtl_paths, rtl_facts, merged_params, err = _prepare_gap_analysis(args)
    if err is not None:
        return err

    from coverage_agent.exclude.candidates import UNEXPLAINED_GAP

    candidates = suggest_excludes(all_bins, rtl_facts, merged_params, scope=args.scope or "")
    real_gaps = [c for c in candidates if c.disposition == UNEXPLAINED_GAP]

    project = config.get("project", os.path.basename(args.config))
    if not real_gaps:
        print(f"No real gaps found for project={project} (everything is either fully covered or RTL-justified as dead).")
        return 0

    try:
        from coverage_agent.llm import format_stimulus_console, suggest_stimulus
    except ImportError as e:
        print(
            f"error: suggest-stimulus requires the 'anthropic' and 'pydantic' packages "
            f"(pip install anthropic pydantic): {e}",
            file=sys.stderr,
        )
        return 2

    print(f"Asking {args.llm_model} for stimulus suggestions on {len(real_gaps)} real gap(s)...")
    rtl_texts = _read_rtl_texts(rtl_paths)
    try:
        suggestions = suggest_stimulus(real_gaps, rtl_texts, merged_params, model=args.llm_model)
    except Exception as e:  # noqa: BLE001 — surface any SDK/auth/API error, don't crash the run
        print(f"error: stimulus suggestion failed: {e}", file=sys.stderr)
        return 1

    print(format_stimulus_console(real_gaps, suggestions))

    if args.json:
        import json as _json

        payload = [
            {
                "instance": c.instance,
                "signal": c.signal,
                "gap_kind": c.gap_kind,
                "suggestion": suggestions[c.candidate_id].model_dump() if c.candidate_id in suggestions else None,
            }
            for c in real_gaps
        ]
        with open(args.json, "w") as f:
            _json.dump(payload, f, indent=2)
        print(f"\nWrote JSON: {args.json}")

    return 0


def _load_exclusions_under_review(path: str, include_unapproved: bool) -> tuple[list[tuple[str, str]], int]:
    """Returns ((instance, signal) pairs to verify, count skipped for
    being unapproved). Accepts either a review JSON (from
    suggest-excludes — filtered to approved==true unless
    include_unapproved) or a real generated .el exclusion file
    (everything in it is, by definition, already "added")."""
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()

    try:
        entries = json.loads(text)
    except json.JSONDecodeError:
        return parse_ucm_exclusion_file(text), 0

    pairs = []
    skipped = 0
    for e in entries:
        if not include_unapproved and e.get("approved") is not True:
            skipped += 1
            continue
        pairs.append((e["instance"], e["signal"]))
    return pairs, skipped


def cmd_verify_excludes(args: argparse.Namespace) -> int:
    all_bins, config, rtl_paths, rtl_facts, merged_params, err = _prepare_gap_analysis(args)
    if err is not None:
        return err

    try:
        exclusions, n_skipped = _load_exclusions_under_review(args.exclude_file, args.all)
    except (OSError, KeyError) as e:
        print(f"error: could not read exclude file {args.exclude_file}: {e}", file=sys.stderr)
        return 2

    if not exclusions:
        msg = "No exclusions to verify"
        if n_skipped:
            msg += f" ({n_skipped} entries skipped — not approved; pass --all to check them too)"
        print(msg)
        return 0

    results = verify_exclusions(exclusions, all_bins, rtl_facts, merged_params)
    print(format_verification_console(results))
    if n_skipped:
        print(f"\n({n_skipped} entries in {args.exclude_file} skipped — not approved; pass --all to check them too)")

    needs_attention = sum(1 for r in results if r.verdict in (SUSPICIOUS, UNKNOWN_SIGNAL))
    return 1 if needs_attention else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="coverage_agent",
        description="Analyze VCS urg toggle coverage reports for gaps (VIP-scoped).",
    )
    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("analyze", help="Parse urg toggle report(s) and print a gap report")
    a.add_argument("path", help="urg report file or directory (searched recursively for .html/.txt)")
    a.add_argument(
        "--scope",
        default="",
        help="Restrict to instances whose hierarchy starts with this prefix, e.g. tb_top.dut.u_axi_vip",
    )
    a.add_argument("--top", type=int, default=50, help="Max signals to list per category (default 50)")
    a.add_argument("--json", metavar="FILE", help="Write full gap report as JSON to FILE")
    a.add_argument("--csv", metavar="FILE", help="Write full gap report as CSV to FILE")
    a.set_defaults(func=cmd_analyze)

    s = sub.add_parser(
        "suggest-excludes",
        help="Cross-reference toggle gaps against RTL to suggest exclusions (human approval required)",
    )
    s.add_argument("report", help="urg toggle report file or directory")
    s.add_argument("--config", required=True, help="Derivative config JSON (project, ip_version, params)")
    s.add_argument(
        "--rtl",
        nargs="+",
        help="RTL file(s)/dir(s) to scan. If omitted, resolved from the config's \"rtl_file\" field.",
    )
    s.add_argument("--scope", default="", help="Restrict to instances whose hierarchy starts with this prefix")
    s.add_argument("--out", default="exclude_candidates.json", help="Where to write the review file")
    s.add_argument(
        "--llm",
        action="store_true",
        help="Ask Claude to judge gaps the RTL scanner found no pattern for (opt-in; "
        "requires 'pip install anthropic pydantic' + API credentials; never a substitute "
        "for the RTL scanner — see README.md)",
    )
    s.add_argument("--llm-model", default="claude-opus-5", help="Model to use with --llm (default: claude-opus-5)")
    s.set_defaults(func=cmd_suggest_excludes)

    g = sub.add_parser(
        "gen-excludes",
        help="Render human-approved entries from a review file into a VCS-style exclusion file",
    )
    g.add_argument("review_file", help="Review file produced by 'suggest-excludes' (after you edit it)")
    g.add_argument("--out", default="exclude.el", help="Output exclusion file path")
    g.set_defaults(func=cmd_gen_excludes)

    st = sub.add_parser(
        "suggest-stimulus",
        help="Suggest stimulus/sequences to close real (non-excludable) toggle coverage gaps",
    )
    st.add_argument("report", help="urg toggle report file or directory")
    st.add_argument("--config", required=True, help="Derivative config JSON (project, ip_version, params)")
    st.add_argument(
        "--rtl",
        nargs="+",
        help="RTL file(s)/dir(s) to scan. If omitted, resolved from the config's \"rtl_file\" field.",
    )
    st.add_argument("--scope", default="", help="Restrict to instances whose hierarchy starts with this prefix")
    st.add_argument(
        "--llm-model", default="claude-opus-5", help="Model to use (default: claude-opus-5)"
    )
    st.add_argument("--json", metavar="FILE", help="Write suggestions as JSON to FILE")
    st.set_defaults(func=cmd_suggest_stimulus)

    v = sub.add_parser(
        "verify-excludes",
        help="Re-check exclusions (a review JSON or a real .el file) against RTL — catch ones added incorrectly",
    )
    v.add_argument("exclude_file", help="Review JSON from suggest-excludes, or a generated/hand-edited .el file")
    v.add_argument("--report", required=True, help="urg toggle report file or directory")
    v.add_argument("--config", required=True, help="Derivative config JSON (project, ip_version, params)")
    v.add_argument(
        "--rtl",
        nargs="+",
        help="RTL file(s)/dir(s) to scan. If omitted, resolved from the config's \"rtl_file\" field.",
    )
    v.add_argument("--scope", default="", help="Restrict to instances whose hierarchy starts with this prefix")
    v.add_argument(
        "--all",
        action="store_true",
        help="When exclude_file is a review JSON, also check unapproved entries (default: approved only)",
    )
    v.set_defaults(func=cmd_verify_excludes)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
