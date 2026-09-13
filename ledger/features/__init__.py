"""Feature definitions, dynamic CAF math, vectorized bitemporal ASOF engine, and registry."""

from ledger.features.caf import (
    adjusted_close_as_of,
    compute_caf_matrix,
    compute_caf_scalar,
)
from ledger.features.engine import (
    ObservationMatrix,
    compute_features_as_of,
    join_features_as_of,
    join_single_feature_as_of,
    validate_observation_matrix,
)
from ledger.features.registry import (
    CyclicDependencyError,
    DuplicateFeatureError,
    FeatureContext,
    FeatureDefinition,
    FeatureNotFoundError,
    FeatureRegistry,
    FeatureRegistryError,
    get_global_registry,
    register,
)

__all__ = [
    "CyclicDependencyError",
    "DuplicateFeatureError",
    "FeatureContext",
    "FeatureDefinition",
    "FeatureNotFoundError",
    "FeatureRegistry",
    "FeatureRegistryError",
    "ObservationMatrix",
    "adjusted_close_as_of",
    "compute_caf_matrix",
    "compute_caf_scalar",
    "compute_features_as_of",
    "get_global_registry",
    "join_features_as_of",
    "join_single_feature_as_of",
    "register",
    "validate_observation_matrix",
]
