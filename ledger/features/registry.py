"""Declarative Feature Registry, DAG dependency resolution, and version hashing.

This module provides:
1. `FeatureContext`: Encapsulates execution inputs (observations, prices, corporate actions).
2. `FeatureDefinition`: Declarative metadata model with source-code & version hashing.
3. `FeatureRegistry`: DAG dependency validator, cycle detector (Kahn's algorithm),
   and topological execution order resolver.
4. `@register`: Function decorator for automatic feature registration.
"""

from __future__ import annotations

import hashlib
import inspect
from collections import defaultdict, deque
from collections.abc import Callable, Sequence
from typing import Any, TypeAlias, overload

import polars as pl
from pydantic import BaseModel, ConfigDict, Field

FeatureComputeFn: TypeAlias = Callable[["FeatureContext"], pl.DataFrame]


# =============================================================================
# 1. Custom Exceptions
# =============================================================================


class FeatureRegistryError(Exception):
    """Base exception for all feature registry errors."""


class FeatureNotFoundError(FeatureRegistryError):
    """Raised when a requested feature is not registered."""


class CyclicDependencyError(FeatureRegistryError):
    """Raised when a circular dependency is detected in the feature DAG."""


class DuplicateFeatureError(FeatureRegistryError):
    """Raised when registering a feature that already exists without overwrite=True."""


# =============================================================================
# 2. Context & Definition Models
# =============================================================================


class FeatureContext(BaseModel):
    """Execution context passed to feature compute functions.

    Bundles common analytical inputs so feature definitions maintain a uniform signature
    and can easily accept new domain tables (e.g. fundamentals, order book metrics)
    without altering downstream function interfaces.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="allow")

    observations: pl.DataFrame | None = Field(
        default=None,
        description="Observation matrix DataFrame [sec_id, observation_timestamp].",
    )
    prices: pl.DataFrame | None = Field(
        default=None,
        description="Raw or adjusted price DataFrame [sec_id, trade_date, close, ...].",
    )
    splits: pl.DataFrame | None = Field(
        default=None,
        description="Corporate actions DataFrame [sec_id, ex_date, split_ratio, ...].",
    )
    catalog: Any | None = Field(
        default=None,
        description="Optional LedgerCatalog connection to query analytical views.",
    )
    custom: dict[str, Any] = Field(
        default_factory=dict,
        description="Arbitrary user-provided parameters or intermediate feature DataFrames.",
    )


class FeatureDefinition(BaseModel):
    """Declarative specification of a quantitative feature.

    Invariants:
    1. Every feature has a unique name and semantic version string.
    2. Explicit dependencies declare upstream required features or tables.
    3. `compute_fn` accepts a `FeatureContext` and returns a `pl.DataFrame`.
    4. Deterministic content hashing records exact code and dependency state for lineage.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    name: str = Field(description="Unique identifier for the feature (e.g. 'momentum_20d').")
    version: str = Field(default="1.0.0", description="Semantic version string (e.g. '1.0.0').")
    dependencies: list[str] = Field(
        default_factory=list,
        description="List of feature or table names required before computing this feature.",
    )
    compute_fn: FeatureComputeFn = Field(
        description="Callable executing feature logic over a FeatureContext.",
    )
    description: str = Field(
        default="", description="Human-readable documentation for the feature."
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Categorization tags (e.g. ['technical', 'momentum']).",
    )

    def __call__(self, ctx: FeatureContext) -> pl.DataFrame:
        """Allow direct invocation of the feature definition as a callable."""
        return self.compute_fn(ctx)

    def compute_hash(self) -> str:
        """Compute deterministic SHA-256 hash of this feature's specification and source code.

        Used in Week 4 lineage manifests to prove exact reproducibility of feature calculations.
        """
        hasher = hashlib.sha256()
        hasher.update(self.name.encode("utf-8"))
        hasher.update(self.version.encode("utf-8"))

        for dep in sorted(self.dependencies):
            hasher.update(dep.encode("utf-8"))

        # Extract normalized source code
        try:
            source = inspect.getsource(self.compute_fn)
            # Normalize whitespace/line endings
            normalized_source = "\n".join(line.strip() for line in source.strip().splitlines())
            hasher.update(normalized_source.encode("utf-8"))
        except (OSError, TypeError):
            # Fallback for dynamic/built-in callables where source is not inspectable
            qualname = getattr(self.compute_fn, "__qualname__", str(self.compute_fn))
            hasher.update(qualname.encode("utf-8"))

        return f"sha256:{hasher.hexdigest()}"


# =============================================================================
# 3. Feature Registry (DAG Resolution & Topological Sort)
# =============================================================================


class FeatureRegistry:
    """Central catalog managing feature definitions, dependency DAGs, and resolution."""

    def __init__(self) -> None:
        self._features: dict[str, FeatureDefinition] = {}

    @overload
    def register(
        self,
        feature_or_fn: FeatureDefinition,
        *,
        overwrite: bool = False,
    ) -> FeatureDefinition: ...

    @overload
    def register(
        self,
        feature_or_fn: FeatureComputeFn,
    ) -> FeatureDefinition: ...

    @overload
    def register(
        self,
        feature_or_fn: None = None,
        *,
        name: str | None = None,
        version: str = "1.0.0",
        dependencies: list[str] | None = None,
        compute_fn: None = None,
        description: str = "",
        tags: list[str] | None = None,
        overwrite: bool = False,
    ) -> Callable[[FeatureComputeFn], FeatureDefinition]: ...

    @overload
    def register(
        self,
        feature_or_fn: None = None,
        *,
        name: str,
        version: str = "1.0.0",
        dependencies: list[str] | None = None,
        compute_fn: FeatureComputeFn,
        description: str = "",
        tags: list[str] | None = None,
        overwrite: bool = False,
    ) -> FeatureDefinition: ...

    def register(
        self,
        feature_or_fn: FeatureDefinition | FeatureComputeFn | None = None,
        *,
        name: str | None = None,
        version: str = "1.0.0",
        dependencies: list[str] | None = None,
        compute_fn: FeatureComputeFn | None = None,
        description: str = "",
        tags: list[str] | None = None,
        overwrite: bool = False,
    ) -> Any:
        """Register a feature definition directly or as a decorator.

        Usage as method with FeatureDefinition:
            registry.register(feature_def)

        Usage as method with keyword arguments:
            registry.register(name="momentum_20d", compute_fn=fn, dependencies=["adj_close"])

        Usage as decorator:
            @registry.register(name="momentum_20d", version="1.0.0", dependencies=["adj_close"])
            def momentum_20d(ctx: FeatureContext) -> pl.DataFrame:
                ...
        """
        if isinstance(feature_or_fn, FeatureDefinition):
            self._add_feature(feature_or_fn, overwrite=overwrite)
            return feature_or_fn

        if name is not None and compute_fn is not None:
            feat_def = FeatureDefinition(
                name=name,
                version=version,
                dependencies=dependencies or [],
                compute_fn=compute_fn,
                description=description or (compute_fn.__doc__ or "").strip(),
                tags=tags or [],
            )
            self._add_feature(feat_def, overwrite=overwrite)
            return feat_def

        def decorator(fn: FeatureComputeFn) -> FeatureDefinition:
            feat_name = name or fn.__name__
            feat_def = FeatureDefinition(
                name=feat_name,
                version=version,
                dependencies=dependencies or [],
                compute_fn=fn,
                description=description or (fn.__doc__ or "").strip(),
                tags=tags or [],
            )
            self._add_feature(feat_def, overwrite=overwrite)
            return feat_def

        if callable(feature_or_fn) and compute_fn is None and name is None:
            # Decorator used without arguments: @registry.register
            return decorator(feature_or_fn)

        # Decorator used with arguments: @registry.register(...)
        return decorator

    def _add_feature(self, feature: FeatureDefinition, overwrite: bool = False) -> None:
        """Internal helper to insert feature into registry."""
        if feature.name in self._features and not overwrite:
            raise DuplicateFeatureError(
                f"Feature '{feature.name}' is already registered. Use overwrite=True to replace it."
            )
        self._features[feature.name] = feature

    def get(self, name: str) -> FeatureDefinition:
        """Retrieve a registered feature definition by name."""
        if name not in self._features:
            available = sorted(self._features.keys())
            raise FeatureNotFoundError(
                f"Feature '{name}' is not registered. Available features: {available}"
            )
        return self._features[name]

    def has(self, name: str) -> bool:
        """Check if a feature name is registered."""
        return name in self._features

    def list_features(self) -> list[str]:
        """Return a sorted list of all registered feature names."""
        return sorted(self._features.keys())

    def clear(self) -> None:
        """Clear all registered features (useful for test isolation)."""
        self._features.clear()

    def resolve_execution_order(self, target_features: Sequence[str]) -> list[FeatureDefinition]:
        """Resolve the topological execution order for target features using Kahn's algorithm.

        Guarantees:
        1. All upstream dependencies are included in the execution plan.
        2. Every dependency appears strictly BEFORE the features that depend on it.
        3. Cyclic dependencies are detected and rejected with `CyclicDependencyError`.
        4. Missing dependencies are detected and rejected with `FeatureNotFoundError`.

        Args:
            target_features: List of feature names to calculate.

        Returns:
            Ordered list of `FeatureDefinition` objects in valid execution order.
        """
        # Step 1: Collect full dependency subgraph (reachable nodes)
        visited_nodes: set[str] = set()
        queue: deque[str] = deque()

        for target in target_features:
            if target not in self._features:
                raise FeatureNotFoundError(
                    f"Requested target feature '{target}' is not registered."
                )
            if target not in visited_nodes:
                visited_nodes.add(target)
                queue.append(target)

        subgraph_nodes: set[str] = set(visited_nodes)

        while queue:
            current_name = queue.popleft()
            current_def = self._features[current_name]
            for dep in current_def.dependencies:
                # Check if dependency is an external raw table or a registered feature
                if dep not in self._features:
                    raise FeatureNotFoundError(
                        f"Feature '{current_name}' depends on unregistered feature/table '{dep}'."
                    )
                if dep not in subgraph_nodes:
                    subgraph_nodes.add(dep)
                    queue.append(dep)

        # Step 2: Build adjacency graph and in-degree map for subgraph nodes
        # Edge: dependency -> dependent (dependency must be computed before dependent)
        adj: dict[str, list[str]] = defaultdict(list)
        in_degree: dict[str, int] = dict.fromkeys(subgraph_nodes, 0)

        for node in subgraph_nodes:
            node_def = self._features[node]
            for dep in node_def.dependencies:
                adj[dep].append(node)
                in_degree[node] += 1

        # Step 3: Kahn's Algorithm (Topological Sort)
        # Seed queue with nodes having zero in-degree (no unresolved dependencies)
        zero_in_degree = deque(sorted([node for node, deg in in_degree.items() if deg == 0]))
        execution_order: list[str] = []

        while zero_in_degree:
            node = zero_in_degree.popleft()
            execution_order.append(node)

            for dependent in sorted(adj[node]):
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    zero_in_degree.append(dependent)

        # Step 4: Check for cycles (if execution_order length != subgraph_nodes count)
        if len(execution_order) != len(subgraph_nodes):
            unresolved = sorted([node for node, deg in in_degree.items() if deg > 0])
            raise CyclicDependencyError(
                f"Cyclic dependency detected in feature DAG involving features: {unresolved}"
            )

        return [self._features[name] for name in execution_order]

    def compute_feature_hashes(
        self,
        features: Sequence[str] | None = None,
    ) -> dict[str, str]:
        """Compute version hashes for specified features (or all registered features)."""
        target_names = features if features is not None else self.list_features()
        result: dict[str, str] = {}
        for name in target_names:
            feat_def = self.get(name)
            result[name] = feat_def.compute_hash()
        return result


# =============================================================================
# 4. Global Registry Instance & Convenience Decorator
# =============================================================================

_GLOBAL_REGISTRY = FeatureRegistry()


def get_global_registry() -> FeatureRegistry:
    """Return the global default FeatureRegistry singleton."""
    return _GLOBAL_REGISTRY


@overload
def register(
    feature_or_fn: FeatureDefinition,
    *,
    overwrite: bool = False,
) -> FeatureDefinition: ...


@overload
def register(
    feature_or_fn: FeatureComputeFn,
) -> FeatureDefinition: ...


@overload
def register(
    feature_or_fn: None = None,
    *,
    name: str | None = None,
    version: str = "1.0.0",
    dependencies: list[str] | None = None,
    compute_fn: None = None,
    description: str = "",
    tags: list[str] | None = None,
    overwrite: bool = False,
) -> Callable[[FeatureComputeFn], FeatureDefinition]: ...


@overload
def register(
    feature_or_fn: None = None,
    *,
    name: str,
    version: str = "1.0.0",
    dependencies: list[str] | None = None,
    compute_fn: FeatureComputeFn,
    description: str = "",
    tags: list[str] | None = None,
    overwrite: bool = False,
) -> FeatureDefinition: ...


def register(
    feature_or_fn: FeatureDefinition | FeatureComputeFn | None = None,
    *,
    name: str | None = None,
    version: str = "1.0.0",
    dependencies: list[str] | None = None,
    compute_fn: FeatureComputeFn | None = None,
    description: str = "",
    tags: list[str] | None = None,
    overwrite: bool = False,
) -> Any:
    """Decorator to register a feature definition in the global FeatureRegistry.

    Example:
    ```python
    @register(name="momentum_20d", version="1.0.0", dependencies=["adj_close"])
    def momentum_20d(ctx: FeatureContext) -> pl.DataFrame:
        ...
    ```
    """
    if isinstance(feature_or_fn, FeatureDefinition):
        return _GLOBAL_REGISTRY.register(feature_or_fn, overwrite=overwrite)
    if callable(feature_or_fn) and compute_fn is None and name is None:
        return _GLOBAL_REGISTRY.register(feature_or_fn)
    if compute_fn is not None and name is not None:
        return _GLOBAL_REGISTRY.register(
            name=name,
            version=version,
            dependencies=dependencies,
            compute_fn=compute_fn,
            description=description,
            tags=tags,
            overwrite=overwrite,
        )
    return _GLOBAL_REGISTRY.register(
        name=name,
        version=version,
        dependencies=dependencies,
        compute_fn=None,
        description=description,
        tags=tags,
        overwrite=overwrite,
    )
