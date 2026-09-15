import json

import pytest

from scripts.session_audit import main, summarize


def test_cumulative_usage_is_not_summed_and_content_is_redacted():
    def event(total):
        return {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": total,
                        "cached_input_tokens": 800,
                        "output_tokens": 100,
                        "reasoning_output_tokens": 50,
                        "total_tokens": total + 100,
                        "secret": "PRIVATE",
                    }
                },
            },
        }

    rows = [event(1000), event(1000), event(1200)]
    rows += [
        {"type": "response_item", "payload": {"type": "function_call_output", "output": "PRIVATE"}}
    ] * 2
    rows += [
        {"type": "response_item", "payload": {"type": "function_call", "name": "safe_tool"}},
        {
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": [{"text": "PRIVATE"}]},
        },
    ]
    report = summarize(json.dumps(row) for row in rows)
    assert report["usage"]["input_tokens"] == 1200
    assert report["usage"]["uncached_input_tokens"] == 400
    assert report["usage"]["output_tokens"] == 100
    assert report["repeated_identical_outputs"] == 1
    assert report["tool_calls"] == {"safe_tool": 1}
    assert "PRIVATE" not in json.dumps(report)


def test_partial_and_missing_usage_are_unknown():
    report = summarize(
        [
            "{",
            "[]",
            "{}",
            json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": None}}),
        ]
    )
    assert report["usage"] is None
    assert report["events"]["malformed_lines"] == 1
    assert report["events"]["unsupported_records"] == 2


def test_cli_does_not_overwrite(tmp_path, capsys):
    source, target = tmp_path / "input.jsonl", tmp_path / "report.json"
    source.write_text("{}\n")
    assert main(["--input", str(source), "--output", str(target)]) == 0
    assert json.loads(capsys.readouterr().out)["billing_estimate"] is None
    with pytest.raises(FileExistsError):
        main(["--input", str(source), "--output", str(target)])
    assert main(["--input", str(source)]) == 0


def token_snapshot(total):
    return {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": total,
                    "cached_input_tokens": total // 2,
                    "output_tokens": total // 10,
                    "total_tokens": total * 11 // 10,
                }
            },
        },
    }


def turn_context(turn_id):
    return {"type": "turn_context", "payload": {"turn_id": turn_id}}


def test_turn_usage_is_delta_and_repeated_context_does_not_reset_baseline():
    rows = [
        token_snapshot(1000),
        turn_context("selected"),
        token_snapshot(1200),
        turn_context("selected"),
        token_snapshot(1400),
        turn_context("other"),
        token_snapshot(2000),
    ]
    report = summarize(map(json.dumps, rows), turn_id="selected")
    assert report["scope"] == "selected_turn_snapshot_delta"
    assert report["usage"]["input_tokens"] == 400
    assert report["usage"]["cached_input_tokens"] == 200
    assert report["usage"]["uncached_input_tokens"] == 200
    assert report["usage"]["output_tokens"] == 40
    assert report["events"]["turn_context"] == 2


@pytest.mark.parametrize(
    "rows",
    [
        [turn_context("selected"), token_snapshot(1000)],
        [token_snapshot(1000), turn_context("selected"), token_snapshot(100)],
        [token_snapshot(1000), turn_context("selected")],
    ],
)
def test_turn_incomplete_or_reset_usage_is_unknown(rows):
    report = summarize(map(json.dumps, rows), turn_id="selected")
    assert report["usage"] is None
    assert report["usage_status"] == "incomplete_or_reset"


def test_unknown_turn_does_not_silently_report_zero():
    with pytest.raises(ValueError, match="Turn not found"):
        summarize([], turn_id="missing")


def test_output_budget_is_measured_without_disclosing_content():
    row = {
        "type": "response_item",
        "payload": {"type": "function_call_output", "output": "PRIVATE" * 10000},
    }
    report = summarize([json.dumps(row)])
    assert report["tool_output_budget"]["above_64000_characters"] == 1
    assert report["tool_output_budget"]["largest_serialized_characters"] == 70002
    assert "PRIVATE" not in json.dumps(report)


def test_cli_turn_selection_and_unknown_turn(tmp_path, capsys):
    path = tmp_path / "session.jsonl"
    rows = [token_snapshot(100), turn_context("chosen"), token_snapshot(200)]
    path.write_text("\n".join(map(json.dumps, rows)))
    assert main(["--input", str(path), "--turn-id", "chosen"]) == 0
    assert json.loads(capsys.readouterr().out)["usage"]["input_tokens"] == 100
    with pytest.raises(SystemExit) as error:
        main(["--input", str(path), "--turn-id", "missing"])
    assert error.value.code == 2
