"""Feature definitions, dynamic CAF math, and vectorized bitemporal ASOF engine."""

from ledger.features.caf import (
    adjusted_close_as_of,
    compute_caf_matrix,
    compute_caf_scalar,
)
from ledger.features.engine import (
    ObservationMatrix,
    join_features_as_of,
    join_single_feature_as_of,
    validate_observation_matrix,
)

__all__ = [
    "ObservationMatrix",
    "adjusted_close_as_of",
    "compute_caf_matrix",
    "compute_caf_scalar",
    "join_features_as_of",
    "join_single_feature_as_of",
    "validate_observation_matrix",
]
