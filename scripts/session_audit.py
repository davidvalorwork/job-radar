"""Summarize one explicitly supplied Codex JSONL log without printing its private content."""

import argparse
import json
from collections import Counter
from collections.abc import Iterable
from hashlib import sha256
from pathlib import Path
from typing import Any


def summarize(lines: Iterable[str], *, turn_id: str | None = None) -> dict[str, Any]:
    events: Counter[str] = Counter()
    calls: Counter[str] = Counter()
    duplicates: Counter[str] = Counter()
    characters: Counter[str] = Counter()
    usage: dict[str, Any] | None = None
    previous: dict[str, Any] | None = None
    baseline: dict[str, Any] | None = None
    current_turn = None
    found = False
    reset = False
    largest_output = oversized_outputs = 0
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            if turn_id is None or current_turn == turn_id:
                events["malformed_lines"] += 1
            continue
        if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
            if turn_id is None or current_turn == turn_id:
                events["unsupported_records"] += 1
            continue
        kind, payload = record.get("type", "unknown"), record["payload"]
        if kind == "turn_context":
            current_turn = payload.get("turn_id")
            if turn_id is not None and current_turn == turn_id and not found:
                baseline, found = previous, True
        selected = turn_id is None or current_turn == turn_id
        if kind == "event_msg" and payload.get("type") == "token_count":
            info = payload.get("info")
            if isinstance(info, dict) and isinstance(info.get("total_token_usage"), dict):
                # Cumulative snapshots must NOT be summed. Reasoning is a subset of output.
                snapshot: dict[str, Any] = {
                    key: value
                    for key, value in info["total_token_usage"].items()
                    if key.endswith("tokens") and type(value) is int and value >= 0
                }
                snapshot["observed_at"] = record.get("timestamp")
                if selected:
                    if previous and any(
                        key not in previous or value < previous[key]
                        for key, value in snapshot.items()
                        if key.endswith("tokens")
                    ):
                        reset = True
                    usage = snapshot
                previous = snapshot
        if not selected:
            continue
        events[kind] += 1
        if kind != "response_item":
            continue
        item = payload.get("type", "unknown")
        if item in {"function_call", "custom_tool_call"}:
            calls[str(payload.get("name", "unknown"))] += 1
        if item in {"function_call_output", "custom_tool_call_output"}:
            output = json.dumps(payload.get("output", ""), ensure_ascii=False)
            characters["tool_output"] += len(output)
            largest_output = max(largest_output, len(output))
            oversized_outputs += len(output) > 64000
            duplicates[sha256(output.encode()).hexdigest()] += 1
        if item == "message":
            for content in payload.get("content", []):
                if isinstance(content, dict) and isinstance(content.get("text"), str):
                    characters[str(payload.get("role", "unknown"))] += len(content["text"])
    if turn_id is not None:
        if not found:
            raise ValueError("Turn not found in supplied log")
        required = {"input_tokens", "cached_input_tokens", "output_tokens", "total_tokens"}
        if not usage or not baseline or reset or not required <= usage.keys() & baseline.keys():
            usage = None
        else:
            usage = {
                **{
                    key: usage[key] - baseline[key]
                    for key in usage
                    if key.endswith("tokens") and key in baseline
                },
                "observed_at": usage["observed_at"],
                "baseline_observed_at": baseline["observed_at"],
            }
    if usage and "input_tokens" in usage and "cached_input_tokens" in usage:
        usage["uncached_input_tokens"] = max(
            0, usage["input_tokens"] - usage["cached_input_tokens"]
        )
    return {
        "scope": (
            "selected_turn_snapshot_delta"
            if turn_id is not None
            else "entire_supplied_log_snapshot_not_one_campaign"
        ),
        "usage_status": "observed" if usage else "incomplete_or_reset",
        "usage": usage,
        "events": dict(events),
        "tool_calls": dict(calls),
        "serialized_characters": dict(characters),
        "repeated_identical_outputs": sum(n - 1 for n in duplicates.values() if n > 1),
        "tool_output_budget": {
            "largest_serialized_characters": largest_output,
            "above_64000_characters": oversized_outputs,
        },
        "billing_estimate": None,
        "note": (
            "Log counters, not an invoice. Cached input is included in input; "
            "reasoning is included in output. Character counts are not token counts. "
            "Missing usage is unknown, not zero."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--turn-id", help="Measure one turn using cumulative snapshot deltas")
    args = parser.parse_args(argv)
    with args.input.open(encoding="utf-8") as stream:
        try:
            report = summarize(stream, turn_id=args.turn_id)
        except ValueError as error:
            parser.error(str(error))
    rendered = json.dumps(report, ensure_ascii=False, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as output:
            output.write(rendered)
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
