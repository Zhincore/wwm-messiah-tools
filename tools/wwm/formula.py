#!/usr/bin/env python3
"""
Attribute formula files (`<hd_dir>/formula/*.ft`): decode to readable formulas, evaluate.

A file is a graph of nodes (variables, constants, strings, arithmetic chains, function calls) plus the
list of named formulas and of input variables. Layout and semantics: docs/formula-ft.md.

Usage:
  wwm-data formulas <hd_dir> -o OUT       # e.g. <install>/LocalData/Patch/HD/oversea
Writes per file OUT/<name>.txt (`NAME = expression  # default value`) and OUT/<name>.json
({formulas: {name: {expr, default, uses}}, inputs: {name: {default, feeds}}}), plus manifest.json.

  wwm-data flows <lua_dump> -o OUT        # <lua_dump> = output dir of `wwm-lua --unluac`
Writes the damage/heal flows (generated Lua under hexm/common/formula_gen/code) as plain readable Lua,
one OUT/<flow>.lua each; docs/damage-flows.md explains them.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import struct
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable

from .gamedata import game_version

VAR, CHAIN, NEG, CONST, STRING, CALL = 1, 2, 3, 4, 5, 6
NO_NODE = 0xFFFF
# operator codes of a chain; 7 and 8 are inferred from two uses each, the rest are checked against every stored value
OPERATORS = {0: "+", 1: "-", 2: "*", 3: "/", 7: ">", 8: ">=", 9: "=="}
FLOAT_TOLERANCE = 1e-3

Config = Callable[[list], float | None]


class FormulaError(ValueError):
    pass


@dataclass(frozen=True)
class Node:
    kind: int
    value: float            # the value the file stores for the node (result with default inputs)
    args: tuple[int, ...]   # the u16 fields after the value


@dataclass
class Formulas:
    names: list[str]
    nodes: list[Node]
    formulas: dict[str, int]                    # formula name -> root node
    inputs: dict[str, tuple[int, list[int]]]    # input name -> (its variable node, nodes that depend on it)

    def name(self, node: Node) -> str:
        return self.names[node.args[0]]


def parse(data: bytes) -> Formulas:
    version, _hash, names_at, formulas_at, inputs_at, nodes_at = struct.unpack_from("<6I", data, 0)
    if version != 1 or not names_at <= formulas_at <= inputs_at <= nodes_at <= len(data):
        raise FormulaError("not a formula file")
    names = [n.decode("utf-8", "replace") for n in data[names_at:formulas_at].split(b"\0")]
    if names and names[-1] == "":
        names.pop()

    def words(start: int, end: int) -> tuple[int, ...]:
        return struct.unpack_from(f"<{(end - start) // 2}H", data, start)

    table = words(formulas_at, inputs_at)
    formulas = {names[table[1 + 2 * i]]: table[2 + 2 * i] for i in range(table[0])}

    deps = words(inputs_at, nodes_at)
    inputs, pos = {}, 1
    for _ in range(deps[0]):
        name, node, count = deps[pos : pos + 3]
        inputs[names[name]] = (node, list(deps[pos + 3 : pos + 3 + count]))
        pos += 3 + count

    (count,) = struct.unpack_from("<H", data, nodes_at)
    nodes, pos = [], nodes_at + 2
    for index in range(count):
        size, kind, number = struct.unpack_from("<IHH", data, pos)
        if number != index:
            raise FormulaError(f"node {index} is numbered {number}")
        payload = data[pos + 8 : pos + 4 + size]
        (value,) = struct.unpack_from("<f", payload, 0)
        nodes.append(Node(kind, value, struct.unpack_from(f"<{(len(payload) - 4) // 2}H", payload, 4)))
        pos += 4 + size
    return Formulas(names, nodes, formulas, inputs)


def chain_terms(node: Node) -> tuple[int, list[tuple[int, int]]]:
    """First operand of a chain and its (operator, operand) pairs."""
    args = node.args
    return args[1], [(args[i], args[i + 1]) for i in range(2, args[0] + 1, 2)]


def call_args(node: Node) -> tuple[int, ...]:
    return node.args[2 : 2 + node.args[1]]


def text(f: Formulas, index: int) -> str:
    """One node as an expression; a variable is shown by name, not expanded."""
    node = f.nodes[index]
    if node.kind == VAR:
        return f.name(node)
    if node.kind == CONST:
        return f"{node.value:g}"
    if node.kind == STRING:
        return repr(f.name(node))
    if node.kind == NEG:
        return f"-{text(f, node.args[1])}"
    if node.kind == CALL:
        return f"{f.name(node)}({', '.join(text(f, a) for a in call_args(node))})"
    if node.kind == CHAIN:
        first, pairs = chain_terms(node)
        out = text(f, first)
        for op, operand in pairs:
            out += f" {OPERATORS.get(op, f'?{op}?')} {text(f, operand)}"
        return f"({out})"
    return f"?kind{node.kind}?"


def uses(f: Formulas, index: int) -> list[str]:
    """Names of the variables an expression reads, in order of first use."""
    found: dict[str, None] = {}

    def walk(i: int) -> None:
        node = f.nodes[i]
        if node.kind == VAR:
            found.setdefault(f.name(node))
        elif node.kind == NEG:
            walk(node.args[1])
        elif node.kind == CALL:
            for a in call_args(node):
                walk(a)
        elif node.kind == CHAIN:
            first, pairs = chain_terms(node)
            walk(first)
            for _, operand in pairs:
                walk(operand)

    walk(index)
    return list(found)


def operate(op: int, a: float, b: float) -> float:
    if op == 0:
        return a + b
    if op == 1:
        return a - b
    if op == 2:
        return a * b
    if op == 3:
        return a / b if b else math.nan
    if op == 7:
        return float(a > b)
    if op == 8:
        return float(a >= b)
    if op == 9:
        return float(a == b)
    raise FormulaError(f"unknown operator {op}")


def evaluate(f: Formulas, inputs: dict[str, float] | None = None, config: Config | None = None) -> dict[str, float]:
    """
    Value of every formula. Inputs not given keep the value stored in the file. `config` receives the
    evaluated arguments of a config() call (strings as str) and returns the looked-up number, or None to
    keep the stored value; without it every config() keeps its stored value.
    """
    inputs = inputs or {}
    cache: dict[int, float] = {}

    def value(i: int):
        if i in cache:
            return cache[i]
        node = f.nodes[i]
        if node.kind == STRING:
            return f.name(node)
        if node.kind == CONST:
            out = node.value
        elif node.kind == VAR:
            name = f.name(node)
            if name in inputs:
                out = float(inputs[name])
            elif node.args[1] != NO_NODE:
                out = value(node.args[1])
            else:
                out = node.value
        elif node.kind == NEG:
            out = -value(node.args[1])
        elif node.kind == CHAIN:
            first, pairs = chain_terms(node)
            out = value(first)
            for op, operand in pairs:
                out = operate(op, out, value(operand))
        elif node.kind == CALL:
            name, args = f.name(node), [value(a) for a in call_args(node)]
            if name == "config":
                found = config(args) if config else None
                out = node.value if found is None else float(found)
            elif name == "max":
                out = max(args)
            elif name == "min":
                out = min(args)
            elif name == "floor":
                out = float(math.floor(args[0]))
            elif name == "clamp":
                out = min(max(args[0], args[1]), args[2])
            else:
                raise FormulaError(f"unknown function {name}")
        else:
            raise FormulaError(f"unknown node kind {node.kind}")
        cache[i] = out
        return out

    return {name: value(root) for name, root in f.formulas.items()}


def table_config(store) -> Config:
    """
    config() backed by the HD tables: `config(table, field, key[, default])` = that field of the row whose key
    is `key`. A missing table, row or field gives None, so the evaluator keeps the stored value.
    """
    tables: dict[str, dict | None] = {}

    def lookup(args: list) -> float | None:
        table, field, key = args[:3]
        if table not in tables:
            known = table in store.manifest.tables
            tables[table] = {r["_key"]: r for r in store.table(table)["rows"]} if known else None
        row = (tables[table] or {}).get(int(key)) if isinstance(key, float) and not math.isnan(key) else None
        found = row.get(field) if row else None
        return float(found) if isinstance(found, (int, float)) and not isinstance(found, bool) else None

    return lookup


def check(f: Formulas) -> list[str]:
    """Names of formulas whose value with default inputs differs from the value stored in the file."""
    bad = []
    for name, got in evaluate(f).items():
        stored = f.nodes[f.formulas[name]].value
        if math.isnan(got) or math.isnan(stored):
            continue
        if abs(got - stored) > FLOAT_TOLERANCE * max(1.0, abs(got), abs(stored)):
            bad.append(name)
    return bad


def describe(f: Formulas) -> dict:
    """The file as JSON-ready data: every formula as text with what it reads, every input with what it feeds."""
    by_node = {root: name for name, root in f.formulas.items()}
    formulas = {
        name: {"expr": text(f, root), "default": round(f.nodes[root].value, 6), "uses": uses(f, root)}
        for name, root in f.formulas.items()
    }
    inputs = {
        name: {"default": round(f.nodes[node].value, 6), "feeds": sorted({by_node[d] for d in feeds if d in by_node})}
        for name, (node, feeds) in f.inputs.items()
    }
    return {"formulas": formulas, "inputs": inputs}


FLOW_DIR = Path("hexm/common/formula_gen/code")
FLOW_WRAPPER = re.compile(r"\Areturn function\(_ENV\)\n(.*)\nend\s*\Z", re.S)


def plain_flow(source: str) -> str:
    """A generated flow without its `return function(_ENV) ... end` wrapper and `_ENV.` prefixes."""
    wrapped = FLOW_WRAPPER.match(source)
    body = wrapped.group(1) if wrapped else source
    lines = [line[2:] if wrapped and line.startswith("  ") else line for line in body.split("\n")]
    return "\n".join(lines).replace("_ENV.", "") + "\n"


def run_flows(args) -> int:
    source = Path(args.lua_dump) / FLOW_DIR
    files = sorted(source.glob("*.lua"))
    if not files:
        print(f"no flows in {source}", file=sys.stderr)
        return 1
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for path in files:
        (out / path.name).write_text(plain_flow(path.read_text(encoding="utf-8")), encoding="utf-8")
    print(f"{len(files)} flows -> {out}")
    return 0


def add_parser(sub) -> None:
    ap = sub.add_parser("formulas", help="attribute formulas, decoded", description=__doc__,
                        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("hd_dir", help="HD store dir, e.g. <install>/LocalData/Patch/HD/oversea")
    ap.add_argument("-o", "--out", required=True, help="output directory")
    ap.set_defaults(run=run)
    ap = sub.add_parser("flows", help="damage and heal flows as plain Lua", description=__doc__,
                        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("lua_dump", help="decompiled Lua root (output of `wwm-lua --unluac`)")
    ap.add_argument("-o", "--out", required=True, help="output directory")
    ap.set_defaults(run=run_flows)


def run(args) -> int:
    hd = Path(args.hd_dir)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    manifest = {"game_version": game_version(hd), "generated": date.today().isoformat(), "files": {}}
    failed = 0
    for path in sorted((hd / "formula").glob("*.ft")):
        f = parse(path.read_bytes())
        data = describe(f)
        mismatched = check(f)
        failed += len(mismatched)
        (out / f"{path.stem}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        lines = [f"{name} = {d['expr']}  # {d['default']:g}" for name, d in sorted(data["formulas"].items())]
        (out / f"{path.stem}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        manifest["files"][path.stem] = {
            "formulas": len(data["formulas"]), "inputs": len(data["inputs"]), "nodes": len(f.nodes),
            "mismatched": mismatched,
        }
        print(f"{path.stem:22s} {len(data['formulas']):5d} formulas {len(data['inputs']):5d} inputs"
              + (f"  {len(mismatched)} do not reproduce their stored value" if mismatched else ""),
              file=sys.stderr if mismatched else sys.stdout)
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"version {manifest['game_version']} -> {out}/manifest.json")
    return 1 if failed else 0
