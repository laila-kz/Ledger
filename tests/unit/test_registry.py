"""Unit tests for the Declarative Feature Registry and DAG resolution engine.

Covers:
1. FeatureDefinition model validation and content-addressable SHA-256 hashing.
2. FeatureContext instantiation and parameter passing.
3. Registration mechanics (direct object, decorator with args, bare decorator).
4. Duplicate detection and overwrite handling.
5. DAG dependency resolution and topological sort (Kahn's algorithm).
6. Cycle detection (direct, indirect, and self-referencing cycles).
7. Missing dependency detection with informative errors.
8. Execution via FeatureContext.
9. Isolation between custom registry instances and the global registry.
"""

from __future__ import annotations

import polars as pl
import pytest
from pydantic import ValidationError

from ledger.features.registry import (
    CyclicDependencyError,
    DuplicateFeatureError,
    FeatureContext,
    FeatureDefinition,
    FeatureNotFoundError,
    FeatureRegistry,
    get_global_registry,
    register,
)

# =============================================================================
# 1. FeatureDefinition & Hashing Tests
# =============================================================================


class TestFeatureDefinitionAndHashing:
    """Tests for FeatureDefinition model and deterministic lineage hashing."""

    def test_feature_definition_valid(self) -> None:
        def dummy_fn(ctx: FeatureContext) -> pl.DataFrame:
            return pl.DataFrame({"dummy": [1.0]})

        feat = FeatureDefinition(
            name="test_feat",
            version="1.0.0",
            dependencies=["raw_prices"],
            compute_fn=dummy_fn,
            description="A test feature",
            tags=["test"],
        )
        assert feat.name == "test_feat"
        assert feat.version == "1.0.0"
        assert feat.dependencies == ["raw_prices"]
        assert feat.description == "A test feature"
        assert feat.tags == ["test"]

    def test_feature_definition_invalid_missing_fields(self) -> None:
        with pytest.raises(ValidationError):
            FeatureDefinition.model_validate({"name": "test_feat"})

    def test_deterministic_source_hash(self) -> None:
        def dummy_fn(ctx: FeatureContext) -> pl.DataFrame:
            return pl.DataFrame({"val": [42.0]})

        feat1 = FeatureDefinition(name="f1", version="1.0.0", compute_fn=dummy_fn)
        feat2 = FeatureDefinition(name="f1", version="1.0.0", compute_fn=dummy_fn)

        h1 = feat1.compute_hash()
        h2 = feat2.compute_hash()

        assert h1.startswith("sha256:")
        assert h1 == h2

    def test_hash_changes_on_version_change(self) -> None:
        def dummy_fn(ctx: FeatureContext) -> pl.DataFrame:
            return pl.DataFrame({"val": [42.0]})

        feat_v1 = FeatureDefinition(name="f1", version="1.0.0", compute_fn=dummy_fn)
        feat_v2 = FeatureDefinition(name="f1", version="1.1.0", compute_fn=dummy_fn)

        assert feat_v1.compute_hash() != feat_v2.compute_hash()

    def test_hash_changes_on_code_change(self) -> None:
        def fn_a(ctx: FeatureContext) -> pl.DataFrame:
            return pl.DataFrame({"val": [10.0]})

        def fn_b(ctx: FeatureContext) -> pl.DataFrame:
            return pl.DataFrame({"val": [20.0]})

        feat_a = FeatureDefinition(name="f", version="1.0.0", compute_fn=fn_a)
        feat_b = FeatureDefinition(name="f", version="1.0.0", compute_fn=fn_b)

        assert feat_a.compute_hash() != feat_b.compute_hash()

    def test_hash_changes_on_dependencies_change(self) -> None:
        def dummy_fn(ctx: FeatureContext) -> pl.DataFrame:
            return pl.DataFrame()

        feat_no_dep = FeatureDefinition(
            name="f", version="1.0.0", dependencies=[], compute_fn=dummy_fn
        )
        feat_with_dep = FeatureDefinition(
            name="f", version="1.0.0", dependencies=["dep_a"], compute_fn=dummy_fn
        )

        assert feat_no_dep.compute_hash() != feat_with_dep.compute_hash()


# =============================================================================
# 2. FeatureContext Tests
# =============================================================================


class TestFeatureContext:
    """Tests for the execution context model."""

    def test_context_instantiation(self) -> None:
        obs = pl.DataFrame({"sec_id": ["SEC_1"]})
        prices = pl.DataFrame({"close": [100.0]})
        splits = pl.DataFrame({"split_ratio": [4.0]})

        ctx = FeatureContext(
            observations=obs,
            prices=prices,
            splits=splits,
            custom={"window": 20},
        )
        assert ctx.observations is not None
        assert ctx.prices is not None
        assert ctx.splits is not None
        assert ctx.custom["window"] == 20


# =============================================================================
# 3. Registration Mechanics Tests
# =============================================================================


class TestRegistrationMechanics:
    """Tests registering features via object, decorator, and duplicate handling."""

    def test_register_via_object(self) -> None:
        registry = FeatureRegistry()
        feat = FeatureDefinition(
            name="f_direct",
            version="1.0.0",
            compute_fn=lambda ctx: pl.DataFrame(),
        )
        registry.register(feat)
        assert registry.has("f_direct")
        assert registry.get("f_direct").name == "f_direct"
        assert registry.list_features() == ["f_direct"]

    def test_register_via_decorator_with_args(self) -> None:
        registry = FeatureRegistry()

        @registry.register(
            name="momentum_20d", version="1.0.0", dependencies=["adj_close"], tags=["momentum"]
        )
        def momentum_20d_impl(ctx: FeatureContext) -> pl.DataFrame:
            """Compute 20-day momentum."""
            return pl.DataFrame()

        assert registry.has("momentum_20d")
        feat = registry.get("momentum_20d")
        assert feat.name == "momentum_20d"
        assert feat.dependencies == ["adj_close"]
        assert feat.tags == ["momentum"]
        assert feat.description == "Compute 20-day momentum."

    def test_register_via_bare_decorator(self) -> None:
        registry = FeatureRegistry()

        @registry.register
        def volatility_20d(ctx: FeatureContext) -> pl.DataFrame:
            """20-day realized volatility."""
            return pl.DataFrame()

        assert registry.has("volatility_20d")
        feat = registry.get("volatility_20d")
        assert feat.name == "volatility_20d"
        assert feat.description == "20-day realized volatility."

    def test_duplicate_registration_raises(self) -> None:
        registry = FeatureRegistry()

        @registry.register(name="duplicate_feat")
        def fn1(ctx: FeatureContext) -> pl.DataFrame:
            return pl.DataFrame()

        with pytest.raises(DuplicateFeatureError, match="already registered"):

            @registry.register(name="duplicate_feat")
            def fn2(ctx: FeatureContext) -> pl.DataFrame:
                return pl.DataFrame()

    def test_overwrite_registration_allowed(self) -> None:
        registry = FeatureRegistry()

        @registry.register(name="overwritable", version="1.0.0")
        def fn1(ctx: FeatureContext) -> pl.DataFrame:
            return pl.DataFrame({"v": [1]})

        @registry.register(name="overwritable", version="2.0.0", overwrite=True)
        def fn2(ctx: FeatureContext) -> pl.DataFrame:
            return pl.DataFrame({"v": [2]})

        assert registry.get("overwritable").version == "2.0.0"

    def test_get_unregistered_raises(self) -> None:
        registry = FeatureRegistry()
        with pytest.raises(FeatureNotFoundError, match="is not registered"):
            registry.get("non_existent")


# =============================================================================
# 4. DAG Dependency Resolution & Cycle Detection Tests
# =============================================================================


class TestDAGResolutionAndCycleDetection:
    """Tests DAG dependency sorting (Kahn's algorithm) and circular dependency rejection."""

    def test_linear_dependency_resolution(self) -> None:
        registry = FeatureRegistry()

        # Chain: raw_prices -> adj_close -> momentum_20d
        registry.register(name="raw_prices", compute_fn=lambda c: pl.DataFrame())
        registry.register(
            name="adj_close", dependencies=["raw_prices"], compute_fn=lambda c: pl.DataFrame()
        )
        registry.register(
            name="momentum_20d", dependencies=["adj_close"], compute_fn=lambda c: pl.DataFrame()
        )

        order = registry.resolve_execution_order(["momentum_20d"])
        order_names = [f.name for f in order]

        assert order_names == ["raw_prices", "adj_close", "momentum_20d"]

    def test_diamond_dependency_resolution(self) -> None:
        registry = FeatureRegistry()

        # Diamond DAG:
        #          raw_prices
        #              │
        #          adj_close
        #         ┌────┴────┐
        #   momentum_20d  vol_20d
        #         └────┬────┘
        #       composite_sig
        registry.register(name="raw_prices", compute_fn=lambda c: pl.DataFrame())
        registry.register(
            name="adj_close", dependencies=["raw_prices"], compute_fn=lambda c: pl.DataFrame()
        )
        registry.register(
            name="momentum_20d", dependencies=["adj_close"], compute_fn=lambda c: pl.DataFrame()
        )
        registry.register(
            name="vol_20d", dependencies=["adj_close"], compute_fn=lambda c: pl.DataFrame()
        )
        registry.register(
            name="composite_sig",
            dependencies=["momentum_20d", "vol_20d"],
            compute_fn=lambda c: pl.DataFrame(),
        )

        order = registry.resolve_execution_order(["composite_sig"])
        order_names = [f.name for f in order]

        assert order_names[0] == "raw_prices"
        assert order_names[1] == "adj_close"
        assert set(order_names[2:4]) == {"momentum_20d", "vol_20d"}
        assert order_names[4] == "composite_sig"

    def test_subgraph_isolates_unneeded_features(self) -> None:
        registry = FeatureRegistry()

        registry.register(name="f_base", compute_fn=lambda c: pl.DataFrame())
        registry.register(
            name="f_target", dependencies=["f_base"], compute_fn=lambda c: pl.DataFrame()
        )
        registry.register(name="f_unrelated", compute_fn=lambda c: pl.DataFrame())

        order = registry.resolve_execution_order(["f_target"])
        order_names = [f.name for f in order]

        assert "f_unrelated" not in order_names
        assert order_names == ["f_base", "f_target"]

    def test_direct_cycle_raises_error(self) -> None:
        registry = FeatureRegistry()

        # A -> B -> A
        registry.register(
            name="feat_a", dependencies=["feat_b"], compute_fn=lambda c: pl.DataFrame()
        )
        registry.register(
            name="feat_b", dependencies=["feat_a"], compute_fn=lambda c: pl.DataFrame()
        )

        with pytest.raises(CyclicDependencyError, match="Cyclic dependency detected"):
            registry.resolve_execution_order(["feat_a"])

    def test_indirect_cycle_raises_error(self) -> None:
        registry = FeatureRegistry()

        # A -> B -> C -> A
        registry.register(
            name="feat_a", dependencies=["feat_b"], compute_fn=lambda c: pl.DataFrame()
        )
        registry.register(
            name="feat_b", dependencies=["feat_c"], compute_fn=lambda c: pl.DataFrame()
        )
        registry.register(
            name="feat_c", dependencies=["feat_a"], compute_fn=lambda c: pl.DataFrame()
        )

        with pytest.raises(CyclicDependencyError, match="Cyclic dependency detected"):
            registry.resolve_execution_order(["feat_a"])

    def test_self_referencing_cycle_raises_error(self) -> None:
        registry = FeatureRegistry()

        # A -> A
        registry.register(
            name="self_cycle", dependencies=["self_cycle"], compute_fn=lambda c: pl.DataFrame()
        )

        with pytest.raises(CyclicDependencyError, match="Cyclic dependency detected"):
            registry.resolve_execution_order(["self_cycle"])

    def test_missing_dependency_raises_error(self) -> None:
        registry = FeatureRegistry()

        registry.register(
            name="orphan_feat",
            dependencies=["missing_parent"],
            compute_fn=lambda c: pl.DataFrame(),
        )

        with pytest.raises(
            FeatureNotFoundError, match="depends on unregistered feature/table 'missing_parent'"
        ):
            registry.resolve_execution_order(["orphan_feat"])


# =============================================================================
# 5. Pipeline Execution & Hashes Tests
# =============================================================================


class TestPipelineExecutionAndHashes:
    """Tests executing resolved features via FeatureContext and batch hash extraction."""

    def test_feature_compute_execution(self) -> None:
        registry = FeatureRegistry()

        @registry.register(name="simple_return")
        def compute_return(ctx: FeatureContext) -> pl.DataFrame:
            assert ctx.prices is not None
            return ctx.prices.with_columns(
                (pl.col("close") / pl.col("close").shift(1) - 1.0).alias("ret_1d")
            )

        prices_df = pl.DataFrame({"close": [100.0, 105.0, 102.0]})
        ctx = FeatureContext(prices=prices_df)

        feat = registry.get("simple_return")
        result = feat.compute_fn(ctx)

        assert "ret_1d" in result.columns
        assert result["ret_1d"][1] == pytest.approx(0.05)

    def test_compute_feature_hashes(self) -> None:
        registry = FeatureRegistry()

        registry.register(name="feat_1", version="1.0.0", compute_fn=lambda c: pl.DataFrame())
        registry.register(name="feat_2", version="2.0.0", compute_fn=lambda c: pl.DataFrame())

        hashes = registry.compute_feature_hashes()
        assert len(hashes) == 2
        assert "feat_1" in hashes
        assert "feat_2" in hashes
        assert hashes["feat_1"].startswith("sha256:")

    def test_global_registry_isolation_and_decorator(self) -> None:
        global_reg = get_global_registry()
        saved_features = dict(global_reg._features)
        try:
            global_reg.clear()

            @register(name="global_feat_example", version="1.0.0")
            def global_impl(ctx: FeatureContext) -> pl.DataFrame:
                return pl.DataFrame()

            assert global_reg.has("global_feat_example")

            # Custom registry is isolated
            custom_reg = FeatureRegistry()
            assert not custom_reg.has("global_feat_example")
        finally:
            global_reg.clear()
            global_reg._features.update(saved_features)
