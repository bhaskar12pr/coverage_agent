"""Constant-bit analysis of an elaborated design, using pyslang (optional).

Answers one question for the toggle flow: *which bits of which signal,
in which instance, can never change value?*  A bit that is constant in
the elaborated design cannot toggle, so a toggle gap on it is dead by
design — and unlike the regex scanner this follows the constant through
the hierarchy and through logic, e.g. (OpenTitan top_earlgrey):

    prim_pad_attr: always_comb attr_warl_o = '0; attr_warl_o.invert = 1; ...
    pinmux:        assign dio_attr[k] = dio_pad_attr_q[k] & warl_mask;
                   -> dio_attr_o -> top_earlgrey.dio_attr_o
    chip:          .dio_attr_i({dio_attr[...], ...})  -> padring.dio_attr_i

Soundness rule: anything not understood is UNKNOWN, never constant —
flops, black boxes (unknown/ignored modules), loops, functions,
arithmetic on unknown bits, non-constant indices, inout ports,
interfaces, multiple conflicting drivers, combinational cycles. The
analysis can only under-prove; a "constant" verdict is a proof modulo
the input file list and defines being the ones that were simulated.

Requires `pip install pyslang` (tested with 12.x). Nothing else in
coverage_agent imports this module unless --slang is used.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

try:  # optional dependency
    import pyslang
    from pyslang import ast, driver
except ImportError:  # pragma: no cover - exercised by callers
    pyslang = None

Bits = list  # list of 0 / 1 / None (unknown), index 0 = LSB


class SlangUnavailable(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# bit helpers
# ---------------------------------------------------------------------------


def _unknown(n: int) -> Bits:
    return [None] * max(n, 0)


def _from_svint(v, width: int) -> Bits:
    out: Bits = []
    n = v.bitWidth
    for i in range(width):
        if i >= n:
            out.append(0 if not v.isSigned else out[-1] if out else 0)
            continue
        b = v[i]
        out.append(None if b.isUnknown else int(b.value))
    return out


def _from_logic(l, width: int) -> Bits:
    """'0 / '1 / 'x fill."""
    val = None if l.isUnknown else int(l.value)
    return [val] * width


def _resize(bits: Bits, width: int, signed: bool = False) -> Bits:
    if len(bits) >= width:
        return bits[:width]
    fill = bits[-1] if (signed and bits) else 0
    return bits + [fill] * (width - len(bits))


def _and(a, b):
    if a == 0 or b == 0:
        return 0
    if a == 1 and b == 1:
        return 1
    return None


def _or(a, b):
    if a == 1 or b == 1:
        return 1
    if a == 0 and b == 0:
        return 0
    return None


def _xor(a, b):
    if a is None or b is None:
        return None
    return a ^ b


def _not(a):
    return None if a is None else 1 - a


def _merge(a: Bits, b: Bits) -> Bits:
    return [x if x == y else None for x, y in zip(a, b)]


def _known(bits: Bits) -> bool:
    return all(b is not None for b in bits)


def _int(bits: Bits) -> int | None:
    if not _known(bits):
        return None
    return sum(b << i for i, b in enumerate(bits))


# ---------------------------------------------------------------------------
# type layout
# ---------------------------------------------------------------------------


def _type_width(t) -> int:
    try:
        w = t.bitWidth
        if w:
            return w
    except Exception:
        pass
    try:
        c = t.canonicalType
        if c.kind == ast.SymbolKind.FixedSizeUnpackedArrayType:
            return _type_width(c.elementType) * c.range.width
        if c.kind == ast.SymbolKind.UnpackedStructType:
            return sum(_type_width(f.type) for f in c)
    except Exception:
        pass
    return 0


def _range_of(t):
    """ConstantRange of a packed/unpacked array or integral vector, or None."""
    c = t.canonicalType
    try:
        return c.range
    except Exception:
        pass
    try:
        if c.isIntegral:
            return c.getFixedRange() if hasattr(c, "getFixedRange") else None
    except Exception:
        pass
    return None


def _element_type(t):
    c = t.canonicalType
    try:
        return c.elementType
    except Exception:
        return None


def _index_offset(rng, idx: int, elem_width: int) -> int | None:
    """Bit offset of element `idx` within a vector/array with range `rng`."""
    left, right = rng.left, rng.right
    lo, hi = min(left, right), max(left, right)
    if not (lo <= idx <= hi):
        return None
    if left >= right:  # [7:0] little endian: index 0 at LSB
        return (idx - lo) * elem_width
    return (hi - idx) * elem_width  # [0:7]: rightmost index at LSB


def _field_offset(struct_type, field_sym) -> int | None:
    off = getattr(field_sym, "bitOffset", None)
    if off is None:
        return None
    c = struct_type.canonicalType
    if c.kind == ast.SymbolKind.UnpackedStructType:
        # unpacked struct: lay fields out in declaration order from the MSB,
        # like a packed struct, so reads and writes agree
        fields = list(c)
        total = sum(_type_width(f.type) for f in fields)
        acc = 0
        for f in fields:
            w = _type_width(f.type)
            if f.name == field_sym.name:
                return total - acc - w
            acc += w
        return None
    return off


# ---------------------------------------------------------------------------
# result objects
# ---------------------------------------------------------------------------


@dataclass
class ConstProof:
    value: Bits                 # LSB-first, all known
    width: int
    chain: list[str] = field(default_factory=list)  # driver chain, nearest first

    @property
    def value_str(self) -> str:
        v = _int(self.value)
        return f"{self.width}'b" + "".join(str(b) for b in reversed(self.value)) if v is not None else "?"


# ---------------------------------------------------------------------------
# the analysis
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"\.?([A-Za-z_][\w$]*)|\[(\d+)(?::(\d+))?\]")


class ConstAnalysis:
    """Build once per design; query with `row_constant(instance, signal)`."""

    def __init__(self, slang_args: str, hier_map: dict[str, str] | None = None):
        if pyslang is None:
            raise SlangUnavailable("pyslang is not installed — `pip install pyslang`")
        d = driver.Driver()
        d.addStandardArgs()
        args = slang_args
        if "--disable-instance-caching" not in args:
            args += " --disable-instance-caching"
        if "--ignore-unknown-modules" not in args:
            args += " --ignore-unknown-modules"
        if not d.parseCommandLine("slang " + args, driver.CommandLineOptions()):
            raise ValueError(f"pyslang rejected arguments: {slang_args}")
        if not d.processOptions():
            raise ValueError("pyslang: invalid options")
        if not d.parseAllSources():
            raise ValueError("pyslang: failed to parse sources")
        self._driver = d
        self.comp = d.createCompilation()
        diags = self.comp.getAllDiagnostics()
        self.error_count = sum(1 for x in diags if x.isError())
        self._sm = d.sourceManager
        self.hier_map = dict(hier_map or {})

        self.instances: dict[str, object] = {}       # hierarchical path -> InstanceSymbol
        self._drivers: dict[str, list[tuple]] = {}   # symbol path -> drivers
        self._symbols: dict[str, object] = {}        # symbol path -> symbol
        self._memo: dict[str, Bits] = {}
        self._busy: set[str] = set()
        # Fixpoint support (see solve()): values from the previous round,
        # used instead of "unknown" when evaluation re-enters a symbol it is
        # still computing, and a count of such re-entries.
        self._prev: dict[str, Bits] = {}
        self._cycle_hits = 0
        self._proc_memo: dict[int, dict[str, Bits] | None] = {}
        self._procs: list[object] = []
        self.tops = [t.hierarchicalPath for t in self.comp.getRoot().topInstances]
        for top in self.comp.getRoot().topInstances:
            self._index_instance(top)

    # -- indexing ------------------------------------------------------------

    def _loc(self, loc) -> str:
        try:
            return f"{self._sm.getFileName(loc)}:{self._sm.getLineNumber(loc)}"
        except Exception:
            return "?"

    def _add_driver(self, sym, drv: tuple) -> None:
        p = sym.hierarchicalPath
        self._symbols.setdefault(p, sym)
        self._drivers.setdefault(p, []).append(drv)

    def _index_instance(self, inst) -> None:
        self.instances[inst.hierarchicalPath] = inst
        # port connections of this instance: drivers on its own internal symbols / parent nets
        for pc in inst.portConnections:
            port = pc.port
            internal = getattr(port, "internalSymbol", None)
            expr = pc.expression
            if internal is None or port.kind != ast.SymbolKind.Port:
                continue
            if port.direction == ast.ArgumentDirection.In:
                self._add_driver(internal, ("port_in", expr, self._loc(port.location), inst.hierarchicalPath))
            elif port.direction == ast.ArgumentDirection.Out:
                if expr is not None and expr.kind == ast.ExpressionKind.Assignment:
                    for sym, lo, w in self._lvalue_targets(expr.left):
                        self._add_driver(sym, ("port_out", expr.left, internal, lo, w, inst.hierarchicalPath))
            else:  # inout / ref: give up on both sides
                self._add_driver(internal, ("opaque", "inout port"))
                if expr is not None:
                    for sym, lo, w in self._lvalue_targets(expr):
                        self._add_driver(sym, ("opaque", "inout port"))

        def visit(s):
            k = s.kind
            if k == ast.SymbolKind.Instance:
                self._index_instance(s)
                return ast.VisitAction.Skip
            if k == ast.SymbolKind.GenerateBlock and s.isUninstantiated:
                return ast.VisitAction.Skip
            if k == ast.SymbolKind.ContinuousAssign:
                a = s.assignment
                if a.kind == ast.ExpressionKind.Assignment:
                    for sym, lo, w in self._lvalue_targets(a.left):
                        self._add_driver(sym, ("cont", a, self._loc(s.location)))
                return ast.VisitAction.Skip
            if k == ast.SymbolKind.Net:
                init = s.initializer if hasattr(s, "initializer") else None
                if init is not None:
                    self._add_driver(s, ("init", init, self._loc(s.location)))
                return ast.VisitAction.Advance
            if k == ast.SymbolKind.ProceduralBlock:
                self._index_proc(s)
                return ast.VisitAction.Skip
            if k in (ast.SymbolKind.Subroutine, ast.SymbolKind.ClassType):
                return ast.VisitAction.Skip
            if k == ast.SymbolKind.InterfacePort:
                return ast.VisitAction.Skip
            return ast.VisitAction.Advance

        inst.body.visit(visit)

    def _index_proc(self, proc) -> None:
        kind = proc.procedureKind
        comb = kind == ast.ProceduralBlockKind.AlwaysComb
        if kind == ast.ProceduralBlockKind.Always:
            b = proc.body
            if b.kind == ast.StatementKind.Timed and b.timing.kind == ast.TimingControlKind.ImplicitEvent:
                comb = True
        if kind in (ast.ProceduralBlockKind.Initial, ast.ProceduralBlockKind.Final):
            return  # not a continuous driver
        targets = []
        ok = self._collect_targets(proc.body, targets)
        idx = len(self._procs)
        self._procs.append(proc)
        # One driver per symbol per block, spanning every bit the block
        # writes: several assignments to one variable in one block (if /
        # else branches, `m = '0; m.f = 1;`) are one driver, not a
        # multiple-driver conflict.
        spans: dict[str, list] = {}
        for sym, lo, w in targets:
            p = sym.hierarchicalPath
            if p not in spans:
                spans[p] = [sym, lo, w]
                continue
            cur = spans[p]
            if cur[1] is None or lo is None:
                cur[1], cur[2] = None, _type_width(sym.type)
            else:
                a, b = min(cur[1], lo), max(cur[1] + cur[2], lo + w)
                cur[1], cur[2] = a, b - a
        for sym, lo, w in spans.values():
            if comb and ok:
                self._add_driver(sym, ("comb", idx, lo, w, self._loc(proc.location)))
            else:
                self._add_driver(sym, ("opaque", "flop" if not comb else "unsupported always_comb"))

    def _collect_targets(self, stmt, out: list) -> bool:
        """Collect every lvalue written by a statement tree. Returns False
        if the block contains anything the interpreter can't handle."""
        if stmt is None:
            return True
        k = stmt.kind
        S = ast.StatementKind
        if k == S.Block:
            return self._collect_targets(stmt.body, out)
        if k == S.List:
            ok = True
            for s in stmt.list:
                ok = self._collect_targets(s, out) and ok
            return ok
        if k == S.Timed:
            return self._collect_targets(stmt.stmt, out) and stmt.timing.kind == ast.TimingControlKind.ImplicitEvent
        if k == S.ExpressionStatement:
            e = stmt.expr
            if e.kind == ast.ExpressionKind.Assignment:
                out.extend(self._lvalue_targets(e.left))
                return True
            return False  # task/function calls etc.: not interpreted
        if k == S.Conditional:
            a = self._collect_targets(stmt.ifTrue, out)
            b = self._collect_targets(stmt.ifFalse, out)
            return a and b
        if k == S.Case:
            ok = True
            for item in stmt.items:
                ok = self._collect_targets(item.stmt, out) and ok
            return self._collect_targets(stmt.defaultCase, out) and ok
        if k in (S.Empty, S.VariableDeclaration):
            return True
        # loops, calls, waits, disables, etc: collect what we can, but give up
        def v(e):
            if isinstance(e, ast.Expression) and e.kind == ast.ExpressionKind.Assignment:
                out.extend(self._lvalue_targets(e.left))
            return ast.VisitAction.Advance
        try:
            stmt.visit(v)
        except Exception:
            pass
        return False

    def _lvalue_targets(self, e) -> list[tuple]:
        """[(root symbol, bit offset or None if whole/unknown, width)]"""
        E = ast.ExpressionKind
        k = e.kind
        if k == E.Concatenation:
            out = []
            for op in e.operands:
                out.extend(self._lvalue_targets(op))
            return out
        root = self._root_symbol(e)
        if root is None:
            return []
        rng = self._lvalue_range(e)
        if rng is None:
            return [(root, None, _type_width(root.type))]
        return [(root, rng[0], rng[1])]

    def _root_symbol(self, e):
        E = ast.ExpressionKind
        while True:
            k = e.kind
            if k in (E.NamedValue, E.HierarchicalValue):
                return e.symbol
            if k in (E.ElementSelect, E.RangeSelect, E.MemberAccess):
                e = e.value
                continue
            if k == E.Conversion:
                e = e.operand
                continue
            return None

    def _lvalue_range(self, e):
        """(offset, width) of an lvalue within its root symbol, or None."""
        E = ast.ExpressionKind
        k = e.kind
        if k in (E.NamedValue, E.HierarchicalValue):
            return (0, _type_width(e.symbol.type))
        if k == E.Conversion:
            return self._lvalue_range(e.operand)
        base = self._lvalue_range(e.value) if k in (E.ElementSelect, E.RangeSelect, E.MemberAccess) else None
        if base is None:
            return None
        sub = self._select_range(e)
        if sub is None:
            return None
        return (base[0] + sub[0], sub[1])

    def _select_range(self, e):
        """(offset, width) selected by one select expression relative to its operand."""
        E = ast.ExpressionKind
        k = e.kind
        vt = e.value.type
        if k == E.MemberAccess:
            off = _field_offset(vt, e.member)
            return None if off is None else (off, _type_width(e.member.type))
        rng = _range_of(vt)
        if rng is None:
            return None
        et = _element_type(vt)
        ew = _type_width(et) if et is not None else 1
        if k == E.ElementSelect:
            idx = self._const_int(e.selector)
            if idx is None:
                return None
            off = _index_offset(rng, idx, ew)
            return None if off is None else (off, ew)
        if k == E.RangeSelect:
            l, r = self._const_int(e.left), self._const_int(e.right)
            if l is None or r is None:
                return None
            sk = e.selectionKind
            if sk == ast.RangeSelectionKind.Simple:
                a, b = l, r
            elif sk == ast.RangeSelectionKind.IndexedUp:
                a, b = (l + r - 1, l) if rng.left >= rng.right else (l, l + r - 1)
            else:  # IndexedDown
                a, b = (l, l - r + 1) if rng.left >= rng.right else (l - r + 1, l)
            o1, o2 = _index_offset(rng, a, ew), _index_offset(rng, b, ew)
            if o1 is None or o2 is None:
                return None
            lo = min(o1, o2)
            return (lo, abs(o1 - o2) + ew)
        return None

    def _const_int(self, e) -> int | None:
        c = e.constant
        if c is None:
            return None
        try:
            v = c.value
        except Exception:
            return None
        if not isinstance(v, pyslang.SVInt) or v.hasUnknown:
            return None
        bits = _from_svint(v, v.bitWidth)
        n = _int(bits)
        if n is not None and v.isSigned and bits and bits[-1] == 1:
            n -= 1 << len(bits)
        return n

    # -- evaluation ----------------------------------------------------------

    def value_of(self, sym) -> Bits:
        p = sym.hierarchicalPath
        if p in self._memo:
            return self._memo[p]
        width = _type_width(sym.type)
        if p in self._busy:
            # Symbol-level cycle (often not a real bit-level one: e.g. pad
            # attributes -> pad inputs -> strap sampling -> pad attributes).
            # Use the previous round's sound approximation; solve() iterates.
            self._cycle_hits += 1
            return list(self._prev.get(p, _unknown(width)))
        self._busy.add(p)
        try:
            bits = self._compute(sym, p, width)
        finally:
            self._busy.discard(p)
        self._memo[p] = bits
        return bits

    def _compute(self, sym, p: str, width: int) -> Bits:
        # parameters / localparams / constant-valued symbols
        k = sym.kind
        if k == ast.SymbolKind.Parameter:
            try:
                return _from_svint(sym.value.value, width)
            except Exception:
                return _unknown(width)
        drivers = self._drivers.get(p, [])
        bits = _unknown(width)
        written = [0] * width
        for drv in drivers:
            for lo, vals in self._contribution(sym, drv, width):
                for i, v in enumerate(vals):
                    j = lo + i
                    if 0 <= j < width:
                        written[j] += 1
                        bits[j] = v
        # conflicting drivers on the same bit -> unknown
        return [b if written[i] == 1 else None for i, b in enumerate(bits)]

    def _contribution(self, sym, drv, width):
        kind = drv[0]
        if kind == "opaque":
            return [(0, _unknown(width))]
        if kind == "port_in":
            expr = drv[1]
            if expr is None or expr.kind == ast.ExpressionKind.EmptyArgument:
                return []  # unconnected input: undriven -> stays unknown
            return [(0, _resize(self.eval(expr), width))]
        if kind == "port_out":
            lhs, internal, lo, w = drv[1], drv[2], drv[3], drv[4]
            vals = self.value_of(internal)
            return self._write_lvalue(sym, lhs, vals)
        if kind == "init":
            return [(0, _resize(self.eval(drv[1]), width))]
        if kind == "cont":
            a = drv[1]
            rhs = self._eval_rhs(a.right, _type_width(a.left.type))
            return self._write_lvalue(sym, a.left, rhs)
        if kind == "comb":
            idx = drv[1]
            env = self._run_proc(idx)
            if env is None or sym.hierarchicalPath not in env:
                return [(0, _unknown(width))]
            lo, w = drv[2], drv[3]
            vals = env[sym.hierarchicalPath]
            if lo is None:
                return [(0, vals)]
            return [(lo, vals[lo : lo + w])]
        return [(0, _unknown(width))]

    def _write_lvalue(self, sym, lhs, vals: Bits):
        """Split rhs bits over an lvalue (possibly a concatenation) and return
        the parts that land in `sym`."""
        E = ast.ExpressionKind
        if lhs.kind == E.Concatenation:
            out = []
            pos = len(vals)
            for op in lhs.operands:  # MSB first
                w = _type_width(op.type)
                part = vals[pos - w : pos] if pos - w >= 0 else _unknown(w)
                pos -= w
                out.extend(self._write_lvalue(sym, op, part))
            return out
        root = self._root_symbol(lhs)
        if root is None or root.hierarchicalPath != sym.hierarchicalPath:
            return []
        rng = self._lvalue_range(lhs)
        if rng is None:
            return [(0, _unknown(_type_width(sym.type)))]
        return [(rng[0], _resize(vals, rng[1]))]

    def _eval_rhs(self, e, width: int) -> Bits:
        if e.kind == ast.ExpressionKind.UnbasedUnsizedIntegerLiteral:
            return _from_logic(e.literalValue, width)
        return _resize(self.eval(e), width, self._signed(e))

    def _signed(self, e) -> bool:
        try:
            return bool(e.type.isSigned)
        except Exception:
            return False

    def eval(self, e, env: dict | None = None) -> Bits:
        width = _type_width(e.type)
        c = e.constant
        if c is not None:
            try:
                v = c.value
                if isinstance(v, pyslang.SVInt):
                    return _from_svint(v, width)
            except Exception:
                pass
        E = ast.ExpressionKind
        k = e.kind
        try:
            if k == E.UnbasedUnsizedIntegerLiteral:
                return _from_logic(e.literalValue, width)
            if k in (E.NamedValue, E.HierarchicalValue):
                sym = e.symbol
                if env is not None and sym.hierarchicalPath in env:
                    return list(env[sym.hierarchicalPath])
                if sym.kind in (ast.SymbolKind.Variable, ast.SymbolKind.Net, ast.SymbolKind.Parameter,
                                ast.SymbolKind.Port, ast.SymbolKind.FormalArgument):
                    if sym.kind == ast.SymbolKind.Port and getattr(sym, "internalSymbol", None) is not None:
                        sym = sym.internalSymbol
                    return _resize(self.value_of(sym), width)
                return _unknown(width)
            if k == E.Conversion:
                op = e.operand
                if op.kind == E.UnbasedUnsizedIntegerLiteral:
                    return _from_logic(op.literalValue, width)
                return _resize(self.eval(op, env), width, self._signed(op))
            if k in (E.ElementSelect, E.RangeSelect, E.MemberAccess):
                base = self.eval(e.value, env)
                sub = self._select_range(e)
                if sub is None:
                    return _unknown(width)
                lo, w = sub
                return _resize(base[lo : lo + w], width)
            if k == E.Concatenation:
                out: Bits = []
                for op in reversed(list(e.operands)):
                    out.extend(self.eval(op, env))
                return _resize(out, width)
            if k == E.Replication:
                n = self._const_int(e.count)
                if n is None:
                    return _unknown(width)
                return _resize(self.eval(e.concat, env) * n, width)
            if k == E.UnaryOp:
                op = e.op
                U = ast.UnaryOperator
                a = self.eval(e.operand, env)
                if op == U.BitwiseNot:
                    return _resize([_not(x) for x in a], width)
                if op in (U.BitwiseAnd, U.BitwiseNand):
                    r = 1
                    for x in a:
                        r = _and(r, x)
                    return _resize([r if op == U.BitwiseAnd else _not(r)], width)
                if op in (U.BitwiseOr, U.BitwiseNor, U.LogicalNot):
                    r = 0
                    for x in a:
                        r = _or(r, x)
                    return _resize([r if op == U.BitwiseOr else _not(r)], width)
                if op in (U.BitwiseXor, U.BitwiseXnor):
                    r = 0
                    for x in a:
                        r = _xor(r, x)
                    return _resize([r if op == U.BitwiseXor else _not(r)], width)
                if op == U.Plus:
                    return _resize(a, width)
                return _unknown(width)
            if k == E.BinaryOp:
                B = ast.BinaryOperator
                op = e.op
                a, b = self.eval(e.left, env), self.eval(e.right, env)
                if op in (B.BinaryAnd, B.BinaryOr, B.BinaryXor, B.BinaryXnor):
                    n = max(len(a), len(b), width)
                    a, b = _resize(a, n, self._signed(e.left)), _resize(b, n, self._signed(e.right))
                    f = {B.BinaryAnd: _and, B.BinaryOr: _or, B.BinaryXor: _xor,
                         B.BinaryXnor: lambda x, y: _not(_xor(x, y))}[op]
                    return _resize([f(x, y) for x, y in zip(a, b)], width)
                if op in (B.LogicalAnd, B.LogicalOr):
                    ra, rb = 0, 0
                    for x in a:
                        ra = _or(ra, x)
                    for x in b:
                        rb = _or(rb, x)
                    r = _and(ra, rb) if op == B.LogicalAnd else _or(ra, rb)
                    return _resize([r], width)
                ia, ib = _int(a), _int(b)
                if ia is not None and ib is not None:
                    if op == B.Equality:
                        return _resize([int(ia == ib)], width)
                    if op == B.Inequality:
                        return _resize([int(ia != ib)], width)
                return _unknown(width)
            if k in (E.StructuredAssignmentPattern, E.SimpleAssignmentPattern, E.ReplicatedAssignmentPattern):
                return self._eval_pattern(e, width, env)
            if k == E.ConditionalOp:
                conds = list(e.conditions)
                if len(conds) != 1:
                    return _unknown(width)
                cb = self.eval(conds[0].expr, env)
                r = 0
                for x in cb:
                    r = _or(r, x)
                lv = _resize(self.eval(e.left, env), width)
                rv = _resize(self.eval(e.right, env), width)
                if r == 1:
                    return lv
                if r == 0:
                    return rv
                return _merge(lv, rv)
        except Exception:
            return _unknown(width)
        return _unknown(width)

    def _eval_pattern(self, e, width: int, env) -> Bits:
        """'{a: x, default: '0} and '{x, y, z} for packed structs and
        arrays. slang already resolves member / type / default setters
        into `elements`, one per field (declaration order) or per array
        element (left-to-right index order)."""
        t = e.type
        c = t.canonicalType
        elems = list(e.elements)
        out = _unknown(width)
        if c.kind in (ast.SymbolKind.PackedStructType, ast.SymbolKind.UnpackedStructType):
            fields = list(c)
            if len(fields) != len(elems):
                return out
            for f, x in zip(fields, elems):
                fo = _field_offset(t, f)
                fw = _type_width(f.type)
                if fo is None:
                    return _unknown(width)
                out[fo : fo + fw] = self._eval_rhs_env(x, fw, env)
            return out
        rng = _range_of(t)
        et = _element_type(t)
        if rng is None or et is None:
            return out
        ew = _type_width(et)
        step = -1 if rng.left >= rng.right else 1
        if e.kind == ast.ExpressionKind.ReplicatedAssignmentPattern:
            n = self._const_int(e.count)
            if n is None:
                return out
            elems = elems * n
        if len(elems) != rng.width:
            return out
        for i, x in enumerate(elems):
            off = _index_offset(rng, rng.left + i * step, ew)
            if off is None:
                return _unknown(width)
            out[off : off + ew] = self._eval_rhs_env(x, ew, env)
        return out

    # -- always_comb interpreter -------------------------------------------

    def _run_proc(self, idx: int):
        if idx in self._proc_memo:
            if self._proc_memo[idx] is None:
                self._cycle_hits += 1  # re-entered while running
            return self._proc_memo[idx]
        self._proc_memo[idx] = None  # cycle guard
        proc = self._procs[idx]
        targets: list = []
        self._collect_targets(proc.body, targets)
        env = {}
        for sym, lo, w in targets:
            env[sym.hierarchicalPath] = _unknown(_type_width(sym.type))
        try:
            env = self._exec(proc.body, env)
        except Exception:
            env = None
        self._proc_memo[idx] = env
        return env

    def _exec(self, stmt, env: dict) -> dict:
        if stmt is None:
            return env
        S = ast.StatementKind
        k = stmt.kind
        if k == S.Block:
            return self._exec(stmt.body, env)
        if k == S.List:
            for s in stmt.list:
                env = self._exec(s, env)
            return env
        if k == S.Timed:
            return self._exec(stmt.stmt, env)
        if k in (S.Empty, S.VariableDeclaration):
            return env
        if k == S.ExpressionStatement:
            e = stmt.expr
            if e.kind != ast.ExpressionKind.Assignment:
                raise ValueError("unsupported statement")
            rhs = self._eval_rhs_env(e.right, _type_width(e.left.type), env)
            env = dict(env)
            for sym, lo, w in self._lvalue_targets(e.left):
                p = sym.hierarchicalPath
                cur = list(env.get(p, _unknown(_type_width(sym.type))))
                for lo2, vals in self._write_lvalue(sym, e.left, rhs):
                    cur[lo2 : lo2 + len(vals)] = vals
                env[p] = cur
            return env
        if k == S.Conditional:
            conds = list(stmt.conditions)
            if len(conds) != 1 or conds[0].pattern is not None:
                raise ValueError("unsupported if")
            cb = self.eval(conds[0].expr, env)
            r = 0
            for x in cb:
                r = _or(r, x)
            if r == 1:
                return self._exec(stmt.ifTrue, env)
            if r == 0:
                return self._exec(stmt.ifFalse, env)
            return self._merge_env(self._exec(stmt.ifTrue, env), self._exec(stmt.ifFalse, env))
        if k == S.Case:
            sel = _int(self.eval(stmt.expr, env))
            branches = []
            chosen = None
            all_known = sel is not None
            for item in stmt.items:
                vals = [_int(self.eval(x, env)) for x in item.expressions]
                if any(v is None for v in vals):
                    all_known = False
                if all_known and chosen is None and sel in vals:
                    chosen = item.stmt
                branches.append(item.stmt)
            if all_known and stmt.condition == ast.CaseStatementCondition.Normal:
                if chosen is not None:
                    return self._exec(chosen, env)
                return self._exec(stmt.defaultCase, env) if stmt.defaultCase is not None else env
            out = self._exec(stmt.defaultCase, env) if stmt.defaultCase is not None else env
            for b in branches:
                out = self._merge_env(out, self._exec(b, env))
            return out
        raise ValueError(f"unsupported statement {k}")

    def _eval_rhs_env(self, e, width, env):
        if e.kind == ast.ExpressionKind.UnbasedUnsizedIntegerLiteral:
            return _from_logic(e.literalValue, width)
        return _resize(self.eval(e, env), width, self._signed(e))

    @staticmethod
    def _merge_env(a: dict, b: dict) -> dict:
        out = {}
        for p in set(a) | set(b):
            if p in a and p in b:
                out[p] = _merge(a[p], b[p])
            else:
                v = a.get(p) or b.get(p)
                out[p] = _unknown(len(v))
        return out

    # -- fixpoint --------------------------------------------------------------

    def solve(self, syms, max_rounds: int = 8) -> int:
        """Evaluate `syms` to a fixpoint and return the number of rounds.

        Demand-driven evaluation breaks symbol-level cycles by returning a
        placeholder for a symbol still being computed. Round 1 uses
        "unknown" (sound); each later round uses the previous round's
        results, which are themselves sound over-approximations, so every
        round stays sound and can only learn more. Stops when a round
        hits no cycle or changes nothing. Without this, results depend on
        query order: whatever was cached while a placeholder was in use
        stays under-proven."""
        rounds = 0
        while rounds < max_rounds:
            rounds += 1
            self._cycle_hits = 0
            for sym in syms:
                self.value_of(sym)
            if self._cycle_hits == 0:
                break
            snapshot = dict(self._memo)
            if snapshot == self._prev:
                break
            self._prev = snapshot
            self._memo = {}
            self._proc_memo = {}
        return rounds

    def prepare_rows(self, rows) -> int:
        """Resolve [(instance, signal), ...] and solve their symbols
        together, so row_constant() answers are order-independent."""
        syms = []
        seen = set()
        for instance, signal in rows:
            r = self.resolve_row(instance, signal)
            if r is not None and r[0].hierarchicalPath not in seen:
                seen.add(r[0].hierarchicalPath)
                syms.append(r[0])
        return self.solve(syms)

    # -- queries ---------------------------------------------------------------

    def map_instance(self, report_instance: str) -> str:
        for src, dst in sorted(self.hier_map.items(), key=lambda kv: -len(kv[0])):
            if report_instance == src or report_instance.startswith(src + "."):
                return dst + report_instance[len(src):]
        return report_instance

    def _lookup(self, inst, name: str):
        try:
            s = inst.body.find(name)
        except Exception:
            s = None
        if s is not None and s.kind == ast.SymbolKind.Port and getattr(s, "internalSymbol", None) is not None:
            s = s.internalSymbol
        return s

    def resolve_row(self, instance: str, signal: str):
        """Map a urg row (report instance path, "dio_attr_i[0].keep_en[0:0]")
        to (symbol, bit offset, width), or None."""
        inst = self.instances.get(self.map_instance(instance))
        if inst is None:
            return None
        toks = _TOKEN_RE.findall(signal)
        if not toks or not toks[0][0]:
            return None
        sym = self._lookup(inst, toks[0][0])
        if sym is None or not hasattr(sym, "type"):
            return None
        t = sym.type
        off, w = 0, _type_width(t)
        for name, a, b in toks[1:]:
            if name:  # .field
                c = t.canonicalType
                f = None
                try:
                    for fld in c:
                        if fld.name == name:
                            f = fld
                            break
                except Exception:
                    f = None
                if f is None:
                    return None
                fo = _field_offset(t, f)
                if fo is None:
                    return None
                off += fo
                t = f.type
                w = _type_width(t)
                continue
            rng = _range_of(t)
            if rng is None:
                if w == 1 and a == "0" and (b in ("", "0")):
                    continue  # urg prints scalar fields as x[0:0]
                return None
            et = _element_type(t)
            ew = _type_width(et) if et is not None else 1
            hi = int(a)
            lo = int(b) if b != "" else hi
            o1, o2 = _index_offset(rng, hi, ew), _index_offset(rng, lo, ew)
            if o1 is None or o2 is None:
                return None
            if b == "" and et is not None:  # element select: descend into element type
                off += o1
                t = et
                w = ew
            else:  # final bit range
                off += min(o1, o2)
                w = abs(o1 - o2) + ew
                t = None
                break
        return sym, off, w

    def row_constant(self, instance: str, signal: str) -> ConstProof | None:
        r = self.resolve_row(instance, signal)
        if r is None:
            return None
        sym, off, w = r
        vals = self.value_of(sym)[off : off + w]
        if len(vals) != w or not _known(vals):
            return None
        return ConstProof(value=vals, width=w, chain=self.explain(sym, off, w))

    def contradictions(self, bins) -> list[tuple]:
        """Rows that toggled in simulation although the RTL holds them
        constant: [(bin, ConstProof)]. Never an engine-soundness signal on
        its own — on OpenTitan top_earlgrey all 85 such rows were
        testbench `force` / uvm_hdl_deposit on TL-UL and JTAG ports — but
        exactly the list to look at for forced signals or a file list /
        define set that doesn't match what was simulated."""
        toggled = [b for b in bins if b.hit_0_to_1 or b.hit_1_to_0]
        self.prepare_rows([(b.instance, b.signal) for b in toggled])
        out = []
        for b in toggled:
            p = self.row_constant(b.instance, b.signal)
            if p is not None:
                out.append((b, p))
        return out

    def explain(self, sym, off: int, w: int, depth: int = 12) -> list[str]:
        """Driver chain for the first bit of a constant range, followed bit
        by bit (through concatenations, selects, bitwise ops and ports) to
        where the constant originates. Evidence for a human reviewer; the
        proof itself is the evaluation above."""
        chain: list[str] = []
        bit = off
        seen = set()
        while sym is not None and depth > 0:
            depth -= 1
            p = sym.hierarchicalPath
            if (p, bit) in seen:
                break
            seen.add((p, bit))
            nxt = None
            hit = None
            for drv in self._drivers.get(p, []):
                kind = drv[0]
                if kind == "port_in":
                    hit = f"{p}: input port, connected in {drv[3].rsplit('.', 1)[0]}"
                    nxt = self._trace(drv[1], bit, _type_width(sym.type))
                elif kind == "port_out":
                    rng = self._lvalue_range(drv[1])
                    if rng is not None and not (rng[0] <= bit < rng[0] + rng[1]):
                        continue
                    hit = f"{p}: driven by output port {drv[2].hierarchicalPath}"
                    nxt = (drv[2], bit - (rng[0] if rng else 0))
                elif kind in ("cont", "init"):
                    if kind == "cont":
                        a, loc = drv[1], drv[2]
                        rng = self._lvalue_range(a.left)
                        if rng is not None and not (rng[0] <= bit < rng[0] + rng[1]):
                            continue
                        rel = bit - (rng[0] if rng else 0)
                        hit = f"{p}: assign at {loc}"
                        nxt = self._trace(a.right, rel, _type_width(a.left.type))
                    else:
                        hit = f"{p}: net initializer at {drv[2]}"
                        nxt = self._trace(drv[1], bit, _type_width(sym.type))
                elif kind == "comb":
                    lo, w2 = drv[2], drv[3]
                    if lo is not None and not (lo <= bit < lo + w2):
                        continue
                    hit = f"{p}: always_comb at {drv[4]}"
                    nxt = None
                if hit:
                    break
            if hit is None:
                break
            chain.append(hit)
            if nxt is None or isinstance(nxt, str):
                if isinstance(nxt, str):
                    chain.append(nxt)
                break
            sym, bit = nxt
        return chain

    def _trace(self, e, bit: int, ctx_width: int):
        """Which (symbol, bit) does bit `bit` of expression `e` come from?
        Returns (sym, bit), a description string for a literal, or None."""
        E = ast.ExpressionKind
        for _ in range(64):
            k = e.kind
            if e.constant is not None or k in (E.IntegerLiteral, E.UnbasedUnsizedIntegerLiteral):
                return f"constant {str(e.syntax).strip() if e.syntax else '?'} at {self._loc(e.sourceRange.start)}"
            if k in (E.NamedValue, E.HierarchicalValue):
                s = e.symbol
                if s.kind == ast.SymbolKind.Port and getattr(s, "internalSymbol", None) is not None:
                    s = s.internalSymbol
                if s.kind == ast.SymbolKind.Parameter:
                    return f"parameter {s.hierarchicalPath}"
                return (s, bit)
            if k == E.Conversion:
                e = e.operand
                continue
            if k in (E.ElementSelect, E.RangeSelect, E.MemberAccess):
                sub = self._select_range(e)
                if sub is None:
                    return None
                bit += sub[0]
                e = e.value
                continue
            if k == E.Concatenation:
                pos = 0
                ops = list(e.operands)
                for op in reversed(ops):  # LSB operand last
                    w = _type_width(op.type)
                    if pos <= bit < pos + w:
                        e, bit = op, bit - pos
                        break
                    pos += w
                else:
                    return None
                continue
            if k == E.BinaryOp:
                B = ast.BinaryOperator
                l, r = self.eval(e.left), self.eval(e.right)
                want = 0 if e.op == B.BinaryAnd else 1 if e.op == B.BinaryOr else None
                lb = l[bit] if bit < len(l) else None
                rb = r[bit] if bit < len(r) else None
                if want is not None and lb == want:
                    e = e.left
                elif want is not None and rb == want:
                    e = e.right
                elif lb is not None:
                    e = e.left
                else:
                    e = e.right
                continue
            if k == E.UnaryOp:
                e = e.operand
                continue
            if k == E.ConditionalOp:
                conds = list(e.conditions)
                cb = self.eval(conds[0].expr) if len(conds) == 1 else [None]
                r = 0
                for x in cb:
                    r = _or(r, x)
                e = e.left if r == 1 else e.right
                continue
            return None
        return None

    def _first_ref(self, e, prefer_const: bool = False):
        found = []

        def v(x):
            if isinstance(x, ast.Expression) and x.kind in (ast.ExpressionKind.NamedValue,
                                                            ast.ExpressionKind.HierarchicalValue):
                s = x.symbol
                if s.kind in (ast.SymbolKind.Variable, ast.SymbolKind.Net):
                    found.append(s)
            return ast.VisitAction.Advance

        try:
            e.visit(v)
        except Exception:
            return None
        if prefer_const:
            for s in found:
                if any(b is not None for b in self.value_of(s)):
                    return s
        return found[0] if found else None
