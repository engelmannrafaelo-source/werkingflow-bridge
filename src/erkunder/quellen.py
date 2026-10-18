"""Channel manifest from validated text measurement derivatives."""
import csv
from pathlib import Path

from .models import Auftrag


def kanalmanifest(entry: Path, order: Auftrag) -> dict[str, list[str]]:
    manifest: dict[str, list[str]] = {}
    for file in order.dateien:
        if not file.ziel.startswith("messdaten/") or not file.ziel.endswith(".csv"):
            continue
        channels: set[str] = set()
        with (entry / file.ziel).open(encoding="utf-8", newline="") as source:
            reader = csv.DictReader(source)
            if "sensor_id" not in (reader.fieldnames or []):
                raise ValueError(f"Quellenmanifest: sensor_id fehlt in {file.ziel}")
            for row in reader:
                channel = row["sensor_id"]
                if not channel or not channel.strip():
                    raise ValueError(f"Quellenmanifest: ungueltiger Messkanal in {file.ziel}")
                channels.add(channel)
        manifest[file.ziel] = sorted(channels)
    return manifest
