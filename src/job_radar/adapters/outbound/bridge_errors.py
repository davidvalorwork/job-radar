"""Small allowlist of non-sensitive local browser failure codes.

Never forward arbitrary provider exceptions, DOM text, addresses or URLs to logs.
The extension's protocol is checked against this list by a regression test.
"""

ERROR_CODES = frozenset(
    {
        "unknown_pagination",
        "search_not_ready",
        "ui_timeout",
        "no_next_page",
        "ambiguous_or_missing_control",
        "account_mismatch",
        "expired_request",
        "open_compose_needs_review",
        "incomplete_conversation",
        "disable_conversation_view_then_retry",
        "repeated_search_page",
        "incomplete_history",
        "history_too_large",
        "browser_script_failed",
        "browser_operation_failed",
        "local_bridge_unavailable",
        "bridge_needs_review",
        "invalid_original_link",
        "invalid_original",
        "original_tab_missing",
        "original_tab_opener_mismatch",
        "original_tab_reused",
        "extension_reload_required",
        "ambiguous_original_tab",
        "original_too_large",
        "invalid_pairing",
        "invalid_or_expired_request",
        "invalid_query",
        "unsupported_query",
        "invalid_send_payload",
        "message_not_observed_this_session",
        "sent_message_not_found",
        "sending_disabled",
        "send_not_authorized",
        "invalid_pdf",
        "missing_compose_dialog",
        "recipient_mismatch",
        "compose_not_ready",
        "unexpected_cc_bcc",
        "sent_conversation_needs_review",
        "draft_not_saved",
        "unsupported_action",
    }
)
PHASES = frozenset(
    {
        "profile",
        "search",
        "page",
        "next",
        "open",
        "original",
        "back",
        "compose",
        "draft",
        "send",
        "read_original",
        "dispatch",
    }
)


def safe_error(value: object) -> str:
    return value if isinstance(value, str) and value in ERROR_CODES else "browser_operation_failed"


def safe_phase(value: object) -> str:
    return value if isinstance(value, str) and value in PHASES else "dispatch"
