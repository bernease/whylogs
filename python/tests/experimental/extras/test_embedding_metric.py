import numpy as np
import pandas as pd

import whylogs as why
from whylogs.core.preprocessing import PreprocessedColumn
from whylogs.core.resolvers import MetricSpec, ResolverSpec
from whylogs.core.schema import DeclarativeSchema
from whylogs.experimental.extras.embedding_metric import (
    DistanceFunction,
    EmbeddingConfig,
    EmbeddingMetric,
)


def test_embedding_metric_row() -> None:
    config = EmbeddingConfig(
        references=np.array([[0.01, 0.01, 0.01], [1, 1, 1]]),
        labels=["B", "A"],
        distance_fn=DistanceFunction.euclidean,
    )
    schema = DeclarativeSchema([ResolverSpec(column_name="col1", metrics=[MetricSpec(EmbeddingMetric, config)])])
    sample_data = np.array([[0.1, 0.1, 0.1], [0.6, 0.6, 0.6], [2, 2, 2]])
    profile = why.log(row={"col1": sample_data}, schema=schema)
    view = profile.view()
    column = view.get_column("col1")
    metric = column.get_metric("embedding")
    assert metric.labels == ["A", "B"]
    assert metric.references.value.tolist() == [[1, 1, 1], [0.01, 0.01, 0.01]]

    summary = column.to_summary_dict()
    assert summary["embedding/A_distance:counts/n"] == 3
    assert summary["embedding/B_distance:counts/n"] == 3
    assert summary["embedding/A_distance:distribution/mean"] > 0
    assert summary["embedding/B_distance:distribution/mean"] > 0
    assert summary["embedding/closest:counts/n"] == 3


def test_embedding_metric_pandas() -> None:
    config = EmbeddingConfig(
        references=np.array([[0.01, 0.01, 0.01], [1, 1, 1]]),
        labels=["B", "A"],
        distance_fn=DistanceFunction.euclidean,
    )
    schema = DeclarativeSchema([ResolverSpec(column_name="col1", metrics=[MetricSpec(EmbeddingMetric, config)])])
    column = [
        np.asarray([0.1, 0.1, 0.1]),
        np.asarray([0.6, 0.6, 0.6]),
        np.asarray([2, 2, 2]),
    ]
    profile = why.log(pandas=pd.DataFrame({"col1": column}), schema=schema)
    view = profile.view()
    column = view.get_column("col1")
    metric = column.get_metric("embedding")
    assert metric.labels == ["A", "B"]
    assert metric.references.value.tolist() == [[1, 1, 1], [0.01, 0.01, 0.01]]

    summary = column.to_summary_dict()
    assert summary["embedding/A_distance:counts/n"] == 3
    assert summary["embedding/B_distance:counts/n"] == 3
    assert summary["embedding/A_distance:distribution/mean"] > 0
    assert summary["embedding/B_distance:distribution/mean"] > 0
    assert summary["embedding/closest:counts/n"] == 3


def test_embedding_metric_merge_happy_case() -> None:
    config = EmbeddingConfig(
        references=np.array([[0.01, 0.01, 0.01], [1, 1, 1]]),
        labels=["A", "B"],
        distance_fn=DistanceFunction.euclidean,
    )
    metric1 = EmbeddingMetric.zero(config)
    metric2 = EmbeddingMetric.zero(config)
    data = PreprocessedColumn._process_scalar_value(np.array([[0.1, 0.1, 0.1], [0.6, 0.6, 0.6], [2, 2, 2]]))
    metric1.columnar_update(data)
    metric2.columnar_update(data)
    merged = metric1.merge(metric2)
    summary = merged.to_summary_dict()
    assert summary["A_distance:counts/n"] == 6
    assert summary["B_distance:counts/n"] == 6
    assert summary["A_distance:distribution/mean"] > 0
    assert summary["B_distance:distribution/mean"] > 0
    assert summary["closest:counts/n"] == 6


def test_embedding_metric_serialization() -> None:
    config = EmbeddingConfig(
        references=np.array([[0.01, 0.01, 0.01], [1, 1, 1]]),
        labels=["A", "B"],
        distance_fn=DistanceFunction.euclidean,
    )
    metric = EmbeddingMetric.zero(config)
    data = PreprocessedColumn._process_scalar_value(np.array([[0.1, 0.1, 0.1], [0.6, 0.6, 0.6], [2, 2, 2]]))
    metric.columnar_update(data)
    msg = metric.to_protobuf()
    deserialized = EmbeddingMetric.from_protobuf(msg)

    assert deserialized.namespace == metric.namespace
    assert deserialized.labels == metric.labels  # TODO: Verify order is preserved
    assert deserialized.submetrics["A_distance"]["distribution"].kll.value.get_n() == 3
    assert (
        deserialized.submetrics["B_distance"]["distribution"].mean.value
        == metric.submetrics["B_distance"]["distribution"].mean.value
    )
    assert deserialized.submetrics["closest"]["counts"].n.value == 3
    assert (deserialized.references.value == metric.references.value).all()
