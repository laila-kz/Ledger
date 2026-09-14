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
        if name == "shift" and node.args and self._is_negative(node.args[0]):
            self._add("L001", node, "shift() receives a negative offset")

        if name in {"mean", "std", "fit"} and self._is_unwindowed(node):
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
        slice_node = node.slice
        if isinstance(slice_node, ast.Slice):
            for bound in (slice_node.lower, slice_node.upper, slice_node.step):
                if isinstance(bound, ast.expr) and self._is_negative(bound):
                    self._add("L001", node, "a time-series slice contains a negative bound")
                    break
        elif self._is_negative(slice_node):
            self._add("L001", node, "a time-series index is negative")
        self.generic_visit(node)

    @staticmethod
    def _is_negative(node: ast.expr) -> bool:
        return isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub)

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
    visitor.visit(tree)
    return sorted(visitor.findings, key=lambda finding: (finding.lineno, finding.col_offset))


def lint_file(path: str | Path) -> list[LintFinding]:
    """Read and lint one Python file."""
    file_path = Path(path)
    return lint_source(file_path.read_text(encoding="utf-8"), str(file_path))
