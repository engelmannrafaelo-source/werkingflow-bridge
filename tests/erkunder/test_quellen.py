"""The channel registry is extracted from synthetic native files, including events."""

import csv

import pytest
from src.erkunder.quellen import kanalmanifest
from tests.erkunder.test_leitstand import body


def test_channels_from_native_store_not_parquet_column_names(tmp_path):
    (tmp_path / "messdaten").mkdir()
    (tmp_path / "messdaten/test.csv").write_text(
        "sensor_id,value\npump,0\nflow,1\npump,1\n"
    )
    assert kanalmanifest(tmp_path, body().auftrag) == {
        "messdaten/test.csv": ["flow", "pump"]
    }


@pytest.mark.parametrize(
    "data", [{"value": [1]}, {"sensor_id": [None]}, {"sensor_id": [" "]}]
)
def test_invalid_native_channels_fail_loud(tmp_path, data):
    (tmp_path / "messdaten").mkdir()
    with (tmp_path / "messdaten/test.csv").open("w", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(data)
        writer.writerows(zip(*data.values()))
    with pytest.raises(ValueError, match="Quellenmanifest"):
        kanalmanifest(tmp_path, body().auftrag)
