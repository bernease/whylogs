import logging
import tempfile
from datetime import datetime
from enum import Enum
from typing import Dict, Optional, List

from google.protobuf.message import DecodeError

from whylogs.core.configs import SummaryConfig
from whylogs.core.errors import DeserializationError
from whylogs.core.proto import (
    ChunkHeader,
    ChunkMessage,
    ChunkOffsets,
    ColumnMessage,
    DatasetProfileHeader,
    DatasetProperties,
    MetricComponentMessage,
)
from whylogs.core.stubs import pd
from whylogs.core.utils import read_delimited_protobuf, write_delimited_protobuf
from whylogs.core.view.column_profile_view import ColumnProfileView
from whylogs.core.datatypes import Fractional, Integral, String

logger = logging.getLogger(__name__)


class SummaryType(str, Enum):
    COLUMN = "COLUMN"
    DATASET = "DATASET"


class DatasetProfileView(object):
    _columns: Dict[str, ColumnProfileView]

    def __init__(
        self, *, columns: Dict[str, ColumnProfileView], dataset_timestamp: datetime, creation_timestamp: datetime
    ):
        self._columns = columns.copy()
        self._dataset_timestamp = dataset_timestamp
        self._creation_timestamp = creation_timestamp

    @property
    def dataset_timestamp(self) -> datetime:
        return self._dataset_timestamp

    @property
    def creation_timestamp(self) -> datetime:
        return self._creation_timestamp

    def merge(self, other: "DatasetProfileView") -> "DatasetProfileView":
        all_names = set(self._columns.keys()).union(other._columns.keys())
        merged_columns: Dict[str, ColumnProfileView] = {}
        for n in all_names:
            lhs = self._columns.get(n)
            rhs = other._columns.get(n)

            res = lhs
            if lhs is None:
                res = rhs
            elif rhs is not None:
                res = lhs + rhs
            assert res is not None
            merged_columns[n] = res
        return DatasetProfileView(
            columns=merged_columns,
            dataset_timestamp=self._dataset_timestamp,
            creation_timestamp=self._creation_timestamp,
        )

    def get_column(self, col_name: str) -> Optional[ColumnProfileView]:
        return self._columns.get(col_name)

    def get_columns(self, col_names: Optional[List[str]] = None) -> Dict[str, ColumnProfileView]:
        if col_names:
            return {k: self._columns.get(k, None) for k in col_names}
        else:
            return self._columns

    def write(self, path: str) -> None:
        all_metric_component_names = set()

        # capture the list of all metric component paths
        for col in self._columns.values():
            all_metric_component_names.update(col.get_metric_component_paths())
        metric_name_list = list(all_metric_component_names)
        metric_name_list.sort()
        metric_name_indices: Dict[str, int] = {}
        metric_index_to_name: Dict[int, str] = {}
        for i in range(0, len(metric_name_list)):
            metric_name_indices[metric_name_list[i]] = i
            metric_index_to_name[i] = metric_name_list[i]

        column_chunk_offsets: Dict[str, ChunkOffsets] = {}
        with tempfile.TemporaryFile("w+b") as f:
            for col_name in sorted(self._columns.keys()):
                column_chunk_offsets[col_name] = ChunkOffsets(offsets=[f.tell()])

                col = self._columns[col_name]

                # for a given column, turn it into a ChunkMessage.
                indexed_component_messages: Dict[int, MetricComponentMessage] = {}
                for m_name, m_component in col.to_protobuf().metric_components.items():
                    index = metric_name_indices.get(m_name)
                    if index is None:
                        raise ValueError(f"Missing metric from index mapping. Metric name: {m_name}")
                    indexed_component_messages[index] = m_component

                chunk_msg = ChunkMessage(metric_components=indexed_component_messages)
                chunk_header = ChunkHeader(type=ChunkHeader.ChunkType.COLUMN, length=chunk_msg.ByteSize())
                write_delimited_protobuf(f, chunk_header)
                f.write(chunk_msg.SerializeToString())

            total_len = f.tell()
            f.flush()

            properties = DatasetProperties(
                dataset_timestamp=to_utc_milliseconds(self._dataset_timestamp),
                creation_timestamp=to_utc_milliseconds(self._creation_timestamp),
            )
            dataset_header = DatasetProfileHeader(
                column_offsets=column_chunk_offsets,
                properties=properties,
                length=total_len,
                indexed_names=metric_index_to_name,
            )

            with open(path, "w+b") as out_f:
                write_delimited_protobuf(out_f, dataset_header)

                f.seek(0)
                while f.tell() < total_len:
                    buffer = f.read(1024)
                    out_f.write(buffer)

    @classmethod
    def read(cls, path: str) -> "DatasetProfileView":
        with open(path, "rb") as f:
            header = read_delimited_protobuf(f, DatasetProfileHeader)
            if header is None:
                raise DeserializationError("Unable to detect and read the message header")
            dataset_timestamp = datetime.fromtimestamp(header.properties.dataset_timestamp / 1000.0)
            creation_timestamp = datetime.fromtimestamp(header.properties.creation_timestamp / 1000.0)
            if header is None:
                raise DeserializationError("Missing valid dataset profile header")
            indexed_names = header.indexed_names
            if len(indexed_names) < 1:
                logger.warning("Name index in the header is empty. Possible data corruption")

            start_offset = f.tell()

            columns = {}
            for col_name in sorted(header.column_offsets.keys()):
                col_offsets = header.column_offsets[col_name]
                all_metric_components: Dict[str, MetricComponentMessage] = {}
                for offset in col_offsets.offsets:
                    actual_offset = offset + start_offset
                    chunk_header = read_delimited_protobuf(f, proto_class_name=ChunkHeader, offset=actual_offset)
                    if chunk_header is None:
                        raise DeserializationError(
                            f"Missing or corrupt chunk header for column {col_name}. Offset: {actual_offset}"
                        )
                    if chunk_header.type != ChunkHeader.ChunkType.COLUMN:
                        raise DeserializationError(
                            f"Expecting chunk header type to be {ChunkHeader.ChunkType.COLUMN}, "
                            f"got {chunk_header.type}"
                        )

                    chunk_msg = ChunkMessage()
                    buf = f.read(chunk_header.length)
                    if len(buf) != chunk_header.length:
                        raise IOError(
                            f"Invalid message for {col_name}. Expecting buffer length of {chunk_header.length}, "
                            f"got {len(buf)}. "
                            f"Offset: {actual_offset}"
                        )
                    try:
                        chunk_msg.ParseFromString(buf)
                    except DecodeError:
                        raise DeserializationError(f"Failed to parse protobuf message for column: {col_name}")

                    for idx, metric_component in chunk_msg.metric_components.items():
                        full_name = indexed_names.get(idx)
                        if full_name is None:
                            raise ValueError(f"Missing metric name in the header. Index: {idx}")
                        all_metric_components[full_name] = metric_component

                column_msg = ColumnMessage(metric_components=all_metric_components)
                columns[col_name] = ColumnProfileView.from_protobuf(column_msg)
            return DatasetProfileView(
                columns=columns, dataset_timestamp=dataset_timestamp, creation_timestamp=creation_timestamp
            )

    def to_pandas(self, column_metric: Optional[str] = None, cfg: Optional[SummaryConfig] = None) -> pd.DataFrame:
        all_dicts = []
        for col_name, col in self._columns.items():
            sum_dict = col.to_summary_dict(column_metric=column_metric, cfg=cfg)
            sum_dict["column"] = col_name
            sum_dict["type"] = SummaryType.COLUMN
            all_dicts.append(sum_dict)
        df = pd.DataFrame(all_dicts)
        return df.set_index("column")


def to_utc_milliseconds(timestamp: datetime) -> int:
    return int(timestamp.timestamp() * 1000)
