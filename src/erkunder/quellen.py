"""Channel manifest from the verified native measurement files, never report prose."""
from pathlib import Path

from pyarrow import parquet

from .models import Auftrag


def kanalmanifest(entry: Path, order: Auftrag) -> dict[str, list[str]]:
    manifest: dict[str, list[str]] = {}
    for file in order.dateien:
        if not file.ziel.startswith("messdaten/") or not file.ziel.endswith(".parquet"):
            continue
        channels: set[str] = set()
        with parquet.ParquetFile(entry / file.ziel) as source:
            # Energy's native stores carry channel identities in sensor_id, not column names.
            if "sensor_id" not in source.schema_arrow.names:
                raise ValueError(f"Quellenmanifest: sensor_id fehlt in {file.ziel}")
            for batch in source.iter_batches(columns=["sensor_id"], batch_size=65536):
                for channel in batch.column(0).unique().to_pylist():
                    if not isinstance(channel, str) or not channel.strip():
                        raise ValueError(f"Quellenmanifest: ungültiger Messkanal in {file.ziel}")
                    channels.add(channel)
        manifest[file.ziel] = sorted(channels)
    return manifest
