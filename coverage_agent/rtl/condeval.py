"""Tiny, restricted evaluator for Verilog-ish constant/condition
expressions (parameter arithmetic in bit ranges, generate-if
conditions). Deliberately not a general expression evaluator: it
rejects anything outside a small allowed grammar rather than guessing.
"""

import ast
import re

_ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult)
_ALLOWED_BOOLOPS = (ast.And, ast.Or)
_ALLOWED_CMPOPS = (ast.Eq, ast.NotEq, ast.Gt, ast.Lt, ast.GtE, ast.LtE)


class UnsupportedExpression(ValueError):
    pass


def _verilog_to_python(expr: str) -> str:
    expr = expr.replace("!=", "\0NE\0")
    expr = expr.replace("&&", " and ")
    expr = expr.replace("||", " or ")
    expr = expr.replace("!", " not ")
    expr = expr.replace("\0NE\0", "!=")
    return expr


def _eval_node(node, params: dict):
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, params)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, bool)):
            return node.value
        raise UnsupportedExpression(f"unsupported constant: {node.value!r}")
    if isinstance(node, ast.Name):
        if node.id not in params:
            raise UnsupportedExpression(f"unknown identifier: {node.id}")
        return params[node.id]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _eval_node(node.operand, params)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_eval_node(node.operand, params)
    if isinstance(node, ast.BinOp) and isinstance(node.op, _ALLOWED_BINOPS):
        left = _eval_node(node.left, params)
        right = _eval_node(node.right, params)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        return left * right
    if isinstance(node, ast.BoolOp) and isinstance(node.op, _ALLOWED_BOOLOPS):
        values = [_eval_node(v, params) for v in node.values]
        return all(values) if isinstance(node.op, ast.And) else any(values)
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], _ALLOWED_CMPOPS):
        left = _eval_node(node.left, params)
        right = _eval_node(node.comparators[0], params)
        op = node.ops[0]
        if isinstance(op, ast.Eq):
            return left == right
        if isinstance(op, ast.NotEq):
            return left != right
        if isinstance(op, ast.Gt):
            return left > right
        if isinstance(op, ast.Lt):
            return left < right
        if isinstance(op, ast.GtE):
            return left >= right
        return left <= right
    raise UnsupportedExpression(f"unsupported syntax: {ast.dump(node)}")


def safe_eval(expr: str, params: dict):
    """Evaluate a small arithmetic/boolean Verilog-ish expression using
    only identifiers present in `params`. Raises UnsupportedExpression
    for anything outside the allowed grammar — callers must treat that
    as "cannot classify" rather than guessing."""
    py_expr = _verilog_to_python(expr.strip())
    try:
        tree = ast.parse(py_expr, mode="eval")
    except SyntaxError as e:
        raise UnsupportedExpression(str(e)) from e
    return _eval_node(tree, params)


_INT_TOKEN_RE = re.compile(r"^[A-Za-z_]\w*$")


def eval_int(expr: str, params: dict) -> int:
    value = safe_eval(expr, params)
    if not isinstance(value, int):
        raise UnsupportedExpression(f"expected int, got {value!r}")
    return int(value)


def eval_bool(expr: str, params: dict) -> bool:
    value = safe_eval(expr, params)
    return bool(value)
