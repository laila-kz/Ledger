"""AST-based static checks for common quant alpha leakage patterns."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LintFinding:
    """One potential leakage finding emitted by the AST analyzer."""

    rule: str
    message: str
    advice: str
    lineno: int
    col_offset: int
    snippet: str


# Names whose subscript is not a row-wise time-series lookup. A negative index
# into any of these is ordinary Python idiom, not a lookahead. Anything not
# listed is assumed to be a series, so an unrecognised name fails safe.
_NON_SERIES_ATTRS = frozenset(
    {
        "name",
        "ticker",
        "symbol",
        "label",
        "path",
        "suffix",
        "prefix",
        "key",
        "keys",
        "columns",
        "index",
        "indexes",
        "dtypes",
        "columns_list",
        "version",
        "text",
        "message",
        "msg",
        "reason",
        "value_str",
    }
)

_NON_SERIES_CALLS = frozenset(
    {
        "read_text",
        "read_bytes",
        "strip",
        "lstrip",
        "rstrip",
        "format",
        "join",
        "replace",
        "lower",
        "upper",
        "split",
        "rsplit",
        "getcwd",
        "basename",
        "dirname",
        "stem",
        "suffix",
        "with_suffix",
        "with_name",
    }
)

RULES: dict[str, tuple[str, str]] = {
    "L001": (
        "Negative shift or lookahead slice",
        "Use a non-negative shift and verify that every feature is available at the decision time.",
    ),
    "L002": (
        "Full-sample normalization",
        "Fit statistics on a trailing window or training partition available at that timestamp.",
    ),
    "L003": (
        "Unbounded forward fill",
        "Bound the fill with a limit or use publication-aware known_from intervals.",
    ),
    "L004": (
        "Unsanitized join key",
        "Join on point-in-time keys and enforce a publication/known_from lag bound.",
    ),
}


def _call_name(node: ast.Call) -> str | None:
    """Return the final attribute or function name for a call."""
    function = node.func
    if isinstance(function, ast.Attribute):
        return function.attr
    if isinstance(function, ast.Name):
        return function.id
    return None


def _constant_string(node: ast.expr) -> str | None:
    value = node.value if isinstance(node, ast.Constant) else None
    return value if isinstance(value, str) else None


class LeakageVisitor(ast.NodeVisitor):
    """Collect conservative, source-located leakage findings."""

    def __init__(self, source: str) -> None:
        self.source = source
        self.findings: list[LintFinding] = []
        self._lead_shifts: set[int] = set()

    def _collect_lead_shifts(self, node: ast.AST) -> None:
        """Record every ``shift(-1)`` that is wrapped by an ``.over(...)`` window.

        The wrapper is the *parent* of the shift call, so it cannot be detected
        while visiting the shift itself. A pre-pass over the tree is the only
        way to see both halves, and the distinction matters: windowed
        ``shift(-1)`` is the polars/SQL LEAD() idiom for deriving interval upper
        bounds from append-only rows, which cannot leak, whereas a bare
        ``shift(-1)`` moves a future value onto the current row.
        """
        for child in ast.walk(node):
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr == "over"
                and isinstance(child.func.value, ast.Call)
                and _call_name(child.func.value) == "shift"
            ):
                self._lead_shifts.add(id(child.func.value))

    def _add(self, rule: str, node: ast.AST, message: str) -> None:
        lineno = getattr(node, "lineno", 1)
        col_offset = getattr(node, "col_offset", 0)
        snippet = ast.get_source_segment(self.source, node) or "<source unavailable>"
        title, advice = RULES[rule]
        self.findings.append(
            LintFinding(
                rule=rule,
                message=f"{title}: {message}",
                advice=advice,
                lineno=lineno,
                col_offset=col_offset,
                snippet=snippet,
            )
        )

    def visit_Call(self, node: ast.Call) -> None:
        name = _call_name(node)
        if name == "shift" and self._is_negative_shift(node) and id(node) not in self._lead_shifts:
            # shift(-1) with an .over() window is the polars/SQL LEAD() idiom,
            # used to derive interval upper bounds from append-only rows. Every
            # row is already known at write time, so it cannot leak. shift(-1)
            # on its own does move a value from the future onto the current row.
            self._add("L001", node, "shift() receives a negative offset")

        if (
            name in {"mean", "std", "fit"}
            and self._is_unwindowed(node)
            and not self._is_scalar_mean(node)
        ):
            is_training_fit = name == "fit" and (
                not self._is_standard_scaler(node) or self._uses_training_partition(node)
            )
            if not is_training_fit:
                self._add("L002", node, f".{name}() is not preceded by a bounded window")

        if name == "ffill" and not any(keyword.arg == "limit" for keyword in node.keywords):
            self._add("L003", node, "ffill() has no limit")

        if name in {"join", "merge"} and self._joins_on_filing_date(node):
            self._add("L004", node, f".{name}() uses filing_date without a point-in-time bound")

        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if self._is_time_series_subscript(node):
            slice_node = node.slice
            if isinstance(slice_node, ast.Slice):
                for bound in (slice_node.lower, slice_node.upper, slice_node.step):
                    if isinstance(bound, ast.expr) and self._is_negative(bound):
                        self._add("L001", node, "a time-series slice contains a negative bound")
                        break
            elif self._is_negative(slice_node):
                self._add("L001", node, "a time-series index is negative")
        self.generic_visit(node)

    def _is_time_series_subscript(self, node: ast.Subscript) -> bool:
        """Only audit negative indexing when it could encode row-wise lookahead.

        A bare negative subscript is ambiguous. ``prices[-1]`` on a Series is a
        peek at the final bar; ``ticker[-1]`` is the last character of a
        string, and ``history[-1]`` on a list is ordinary most-recent access
        with no row alignment to leak across. The distinction is the
        container, so this checks what is being indexed.

        An unknown container is treated as time-series, because a leak that
        hides behind an unrecognised name is the expensive failure here.
        """
        container = node.value
        if isinstance(container, ast.Call):
            name = _call_name(container)
            return name not in _NON_SERIES_CALLS
        if isinstance(container, ast.Attribute):
            return container.attr not in _NON_SERIES_ATTRS
        if isinstance(container, ast.Constant) and isinstance(container.value, str):
            return False
        return not isinstance(container, (ast.List, ast.Tuple, ast.Dict, ast.Set))

    @staticmethod
    def _is_negative(node: ast.expr) -> bool:
        return isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub)

    @staticmethod
    def _is_negative_shift(node: ast.Call) -> bool:
        """Return True for a negative shift amount, positional or keyword.

        `shift(-1)` and `shift(periods=-1)` are the same leak, so both spellings
        are read. Only a literal sign is treated as negative: `shift(-period)`
        is a runtime value the analyser cannot resolve, and guessing there would
        mean flagging every parameterised shift.
        """
        for candidate in (*node.args, *(kw.value for kw in node.keywords)):
            if isinstance(candidate, ast.UnaryOp) and isinstance(candidate.op, ast.USub):
                return True
        return False

    @staticmethod
    def _is_windowed(node: ast.Call) -> bool:
        receiver = node.func.value if isinstance(node.func, ast.Attribute) else None
        return isinstance(receiver, ast.Call) and _call_name(receiver) in {
            "rolling",
            "expanding",
            "ewm",
        }

    def _is_unwindowed(self, node: ast.Call) -> bool:
        return not self._is_windowed(node)

    @staticmethod
    def _is_scalar_mean(node: ast.Call) -> bool:
        """Return True for ``x.mean()`` where ``x`` is not a row-wise series.

        ``prices.mean()`` is a full-sample statistic and leaks. ``total.mean()``
        where ``total`` is a scalar aggregate is just arithmetic. The AST cannot
        resolve types, so this only exempts the unambiguous shape: a mean over a
        literal, a binop of two such means, or a mean of a known non-series.
        Anything else stays flagged.
        """
        if not isinstance(node.func, ast.Attribute):
            return False
        return LeakageVisitor._is_known_scalar_receiver(node.func.value)

    @staticmethod
    def _is_known_scalar_receiver(receiver: ast.expr) -> bool:
        """Return True only for receivers whose scalar-ness is visible in syntax.

        Deliberately narrow. A bare Name is excluded because it is exactly the
        ambiguous case: `a.mean()` is arithmetic when `a` is a scalar aggregate
        and a full-sample leak when `a` is a Series, and the AST cannot say
        which. Only literals and a small set of container attributes that never
        hold a time series are treated as known scalars.
        """
        if isinstance(receiver, ast.Constant):
            return True
        if isinstance(receiver, ast.Attribute):
            return receiver.attr in _NON_SERIES_ATTRS
        return False

    @staticmethod
    def _is_standard_scaler(node: ast.Call) -> bool:
        receiver = node.func.value if isinstance(node.func, ast.Attribute) else None
        return isinstance(receiver, ast.Call) and _call_name(receiver) in {
            "StandardScaler",
            "MinMaxScaler",
            "RobustScaler",
        }

    @staticmethod
    def _uses_training_partition(node: ast.Call) -> bool:
        if not node.args or not isinstance(node.args[0], ast.Name):
            return False
        name = node.args[0].id.lower()
        return name.startswith("train_") or name.endswith("_training")

    @staticmethod
    def _joins_on_filing_date(node: ast.Call) -> bool:
        for keyword in node.keywords:
            if keyword.arg in {"on", "left_on", "right_on"}:
                values: list[ast.expr]
                if isinstance(keyword.value, (ast.List, ast.Tuple, ast.Set)):
                    values = [
                        element for element in keyword.value.elts if isinstance(element, ast.expr)
                    ]
                else:
                    values = [keyword.value]
                if any(_constant_string(value) == "filing_date" for value in values):
                    return True
        return False


def lint_source(source: str, filename: str = "<string>") -> list[LintFinding]:
    """Parse and lint Python source, raising SyntaxError for invalid code."""
    tree = ast.parse(source, filename=filename)
    visitor = LeakageVisitor(source)
    visitor._collect_lead_shifts(tree)
    visitor.visit(tree)
    return sorted(visitor.findings, key=lambda finding: (finding.lineno, finding.col_offset))


def lint_file(path: str | Path) -> list[LintFinding]:
    """Read and lint one Python file."""
    file_path = Path(path)
    return lint_source(file_path.read_text(encoding="utf-8"), str(file_path))
