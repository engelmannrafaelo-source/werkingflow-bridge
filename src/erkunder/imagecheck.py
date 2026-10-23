"""Offline deployment gate for the actual isolated runtime image.

Imports alone miss lazy dependencies. Exercise the installed child parser too;
no CLI process, token, network, customer input or conversion is involved.
"""
import sys


def check() -> None:
    import src.sdk_parser
    import src.erkunder.kind
    import src.erkunder.platz
    import src.erkunder.leitstand
    from claude_code_sdk import AssistantMessage, ResultMessage, SystemMessage

    parser = src.erkunder.kind.install_resilient_parser()
    cases = [
        ({"type": "system", "subtype": "init"}, SystemMessage),
        ({"type": "assistant", "message": {
            "model": "synthetic", "content": [{"type": "text", "text": "synthetic"}],
        }}, AssistantMessage),
        ({"type": "result", "subtype": "success", "duration_ms": 1,
          "duration_api_ms": 1, "is_error": False, "num_turns": 1,
          "session_id": "synthetic", "total_cost_usd": 0, "usage": {},
          "result": "synthetic"}, ResultMessage),
    ]
    for message, expected in cases:
        if not isinstance(parser(message), expected):
            raise RuntimeError("Unexpected SDK parser result")
    if parser({"type": "synthetic_unknown_event"}) is not None:
        raise RuntimeError("Unknown SDK event was not skipped")


if __name__ == "__main__":
    try:
        check()
    except Exception as error:
        print("ERKUNDER_IMAGE_CHECK FAIL " + type(error).__name__, file=sys.stderr)
        sys.exit(1)
    print("ERKUNDER_IMAGE_CHECK OK imports and child parser")
