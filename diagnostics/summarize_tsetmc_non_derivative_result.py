"""Summarize the large non-derivative TSETMC diagnostic JSON.

This utility is local and read-only: it reads the existing JSON and prints a
compact UTF-8 JSON report to stdout. It does not call TSETMC, access a database,
import application code, or modify the input file.

Run from the repository root:
    python diagnostics/summarize_tsetmc_non_derivative_result.py
    python diagnostics/summarize_tsetmc_non_derivative_result.py --input diagnostics/non_derivative_missing_result.json > diagnostics/non_derivative_missing_summary.json
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


DEFAULT_INPUT = "diagnostics/non_derivative_missing_result.json"


def as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def status_rows(candidate: dict) -> list[dict]:
    state = candidate.get("instrument_state_all") or {}
    rows = as_list(state.get("state_rows"))
    return [row for row in rows if isinstance(row, dict)]


def state_labels(candidate: dict) -> list[str]:
    labels = []
    for row in status_rows(candidate):
        label = row.get("cEtavalTitle") or row.get("stateTitle") or row.get("state")
        if label is not None and str(label).strip():
            labels.append(str(label).strip())
    return labels


def compact_candidate(candidate: dict) -> dict:
    info = candidate.get("instrument_info") or {}
    state = candidate.get("instrument_state_all") or {}
    exact_fields = info.get("matching_fields") or []
    return {
        "ins_code": candidate.get("ins_code"),
        "source": candidate.get("source"),
        "in_new_market_watch": candidate.get("in_new_market_watch"),
        "in_legacy_market_watch": candidate.get("in_legacy_market_watch"),
        "instrument_info_http": info.get("http_status"),
        "instrument_info_error": info.get("error") or info.get("parse_error"),
        "instrument_info_fields": exact_fields[:2],
        "state_http": state.get("http_status"),
        "state_error": state.get("error") or state.get("parse_error"),
        "state_rows_found": state.get("state_rows_found"),
        "states": state_labels(candidate),
        "state_rows": status_rows(candidate)[:5],
        "state_top_matches": as_list(candidate.get("state_top_matches"))[:5],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=DEFAULT_INPUT,
                        help="Path to the existing diagnostic JSON.")
    args = parser.parse_args()

    path = Path(args.input)
    with path.open("r", encoding="utf-8-sig") as handle:
        source = json.load(handle)

    if not isinstance(source, dict):
        raise SystemExit("Input JSON root must be an object.")

    results = as_list(source.get("results"))
    market_counts = Counter(
        str(row.get("market_board") or "(نامشخص)")
        for row in results if isinstance(row, dict)
    )
    state_counts: Counter[str] = Counter()
    state_code_counts: Counter[str] = Counter()
    candidate_count = 0
    candidates_in_new = 0
    candidates_in_legacy = 0
    info_ok = 0
    state_ok = 0
    exact_symbols_found = 0
    symbols_with_any_state = 0
    symbols_with_state_top = 0
    compact_results = []

    for row in results:
        if not isinstance(row, dict):
            continue
        candidates = as_list(row.get("candidate_instruments"))
        compact_candidates = []
        symbol_states = []
        has_state_top = False
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            candidate_count += 1
            candidates_in_new += bool(candidate.get("in_new_market_watch"))
            candidates_in_legacy += bool(candidate.get("in_legacy_market_watch"))

            info = candidate.get("instrument_info") or {}
            state = candidate.get("instrument_state_all") or {}
            info_ok += info.get("http_status") == 200 and not info.get("error")
            state_ok += state.get("http_status") == 200 and not state.get("error")

            labels = state_labels(candidate)
            symbol_states.extend(labels)
            state_counts.update(labels)

            for state_row in status_rows(candidate):
                code = state_row.get("cEtaval")
                if code is not None:
                    state_code_counts[str(code)] += 1

            top_rows = as_list(candidate.get("state_top_matches"))
            has_state_top = has_state_top or bool(top_rows)
            compact_candidates.append(compact_candidate(candidate))

        if symbol_states:
            symbols_with_any_state += 1
        if has_state_top:
            symbols_with_state_top += 1

        exact = as_list(row.get("exact_symbol_search_matches"))
        if exact:
            exact_symbols_found += 1

        compact_results.append({
            "symbol": row.get("symbol"),
            "legacy_ins_code": row.get("legacy_ins_code"),
            "market_board": row.get("market_board"),
            "search_http_status": row.get("search_http_status"),
            "search_error": row.get("search_error") or row.get("search_parse_error"),
            "exact_symbol_search_matches": exact,
            "candidate_instruments": compact_candidates,
        })

    report = {
        "summary_of": str(path),
        "diagnostic": source.get("diagnostic"),
        "read_only_source": source.get("read_only"),
        "input_symbols": source.get("historical_non_derivative_rows_selected"),
        "symbols_summarized": len(compact_results),
        "excluded_rows": source.get("excluded_rows", []),
        "market_board_counts": dict(market_counts),
        "market_watch_new": source.get("market_watch_new"),
        "market_watch_legacy": {
            key: value for key, value in (source.get("market_watch_legacy") or {}).items()
            if key not in ("url", "codes")
        },
        "instrument_state_top": source.get("instrument_state_top"),
        "aggregate": {
            "symbols_with_exact_symbol_search_match": exact_symbols_found,
            "candidate_inscodes_checked": candidate_count,
            "candidate_inscodes_in_new_market_watch": candidates_in_new,
            "candidate_inscodes_in_legacy_market_watch": candidates_in_legacy,
            "instrument_info_http_200_without_error": info_ok,
            "instrument_state_http_200_without_error": state_ok,
            "symbols_with_state_rows": symbols_with_any_state,
            "symbols_with_state_top_matches": symbols_with_state_top,
            "state_title_frequency": dict(state_counts.most_common()),
            "state_code_frequency": dict(state_code_counts.most_common()),
            "endpoint_errors": source.get("endpoint_errors", []),
        },
        "symbols": compact_results,
        "interpretation_notes": source.get("interpretation_notes", []),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
