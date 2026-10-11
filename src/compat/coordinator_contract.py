"""Compatibility with the current coordinator; domain schemas belong to its app.

This adapter is removed with the separate src/erkunder migration. The job
runner itself transports payloads/results and owns no editorial schema.
"""


def parse_order(payload):
    from src.erkunder.models import Auftrag

    return Auftrag.model_validate(payload)


def parse_result(payload):
    from src.erkunder.models import Ergebnis

    return Ergebnis.model_validate(payload).model_dump(by_alias=True)


def safe_step(step):
    return (
        step
        if step
        in {
            "daten",
            "wiederaufnahme",
            "erkunder-1",
            "erkunder-2",
            "erkunder-3",
            "harmonisierung",
            "pruefung",
            "harmonisierung-korrektur",
            "pruefung-korrektur",
            *(
                f"{kind}-korrektur-{n}"
                for kind in ("harmonisierung", "pruefung")
                for n in range(2, 6)
            ),
        }
        else "unbekannt"
    )
