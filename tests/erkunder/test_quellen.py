"""The channel registry is extracted from synthetic native files, including events."""
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from src.erkunder.quellen import kanalmanifest
from tests.erkunder.test_leitstand import body


def test_channels_from_native_store_not_parquet_column_names(tmp_path):
    (tmp_path / 'messdaten').mkdir()
    pq.write_table(pa.table({'sensor_id': ['pump', 'flow', 'pump'], 'value': [0, 1, 1]}),
                   tmp_path / 'messdaten/test.parquet')
    assert kanalmanifest(tmp_path, body().auftrag) == {'messdaten/test.parquet': ['flow', 'pump']}


@pytest.mark.parametrize('data', [{'value': [1]}, {'sensor_id': [None]}, {'sensor_id': [' ']}])
def test_invalid_native_channels_fail_loud(tmp_path, data):
    (tmp_path / 'messdaten').mkdir()
    pq.write_table(pa.table(data), tmp_path / 'messdaten/test.parquet')
    with pytest.raises(ValueError, match='Quellenmanifest'):
        kanalmanifest(tmp_path, body().auftrag)
