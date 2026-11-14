"""Minimal PEP 503/508 helpers implemented with the standard library.

The target interpreter may not carry ``packaging`` (it ships no pip), so name
normalization, requirement/specifier parsing and environment-marker evaluation
are reimplemented here for the small grammar used by real wheel metadata.
"""

from __future__ import annotations

import re

_NORMALIZE_RE = re.compile(r"[-_.]+")


def normalize_name(name: str) -> str:
    """PEP 503 normalized distribution name."""
    return _NORMALIZE_RE.sub("-", name).strip().lower()


def parse_specifier(spec: str) -> list[tuple[str, str]]:
    """Parse ``==1.2, >=1.0`` into ``[("==", "1.2"), (">=", "1.0")]``."""
    result = []
    spec = spec.strip()
    if not spec:
        return result
    for clause in spec.split(","):
        clause = clause.strip()
        if not clause:
            continue
        match = re.match(r"^(===|==|!=|>=|<=|~=|>|<)\s*([A-Za-z0-9_.+!*-]+)$", clause)
        if not match:
            raise ValueError(f"unsupported version specifier: {clause!r}")
        result.append((match.group(1), match.group(2).strip()))
    return result


def _version_tuple(version: str) -> tuple:
    parts = []
    for chunk in re.split(r"[.+-]", version):
        if chunk.isdigit():
            parts.append((1, int(chunk)))
        else:
            parts.append((0, chunk))
    return tuple(parts)


def specifier_matches(specifiers: list[tuple[str, str]], version: str) -> bool:
    """Version matching for the operators used by the pinned dependency set."""
    current = _version_tuple(version)
    for operator, wanted in specifiers:
        target = _version_tuple(wanted)
        if operator == "==":
            # PEP 440 prefix matching: ==1.2 matches 1.2.1.
            if not (current == target or (
                len(current) > len(target) and current[: len(target)] == target
            )):
                return False
        elif operator == "!=":
            if current == target or (
                len(current) > len(target) and current[: len(target)] == target
            ):
                return False
        elif operator == ">=":
            if current < target:
                return False
        elif operator == "<=":
            if current > target:
                return False
        elif operator == ">":
            if current <= target:
                return False
        elif operator == "<":
            if current >= target:
                return False
        elif operator == "~=":
            if len(target) < 2:
                raise ValueError("~= requires at least two version parts")
            if current < target or current[: len(target) - 1] != target[: len(target) - 1]:
                return False
        elif operator == "===":
            if version != wanted:
                return False
        else:  # pragma: no cover - guarded by parse_specifier
            raise ValueError(f"unsupported operator {operator}")
    return True


_REQ_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*"
    r"(?P<extras>\[[^\]]*\])?\s*"
    r"(?P<spec>[^;]*)"
    r"(?:;(?P<marker>.*))?$"
)


def parse_requirement(text: str) -> dict:
    """Parse one PEP 508 requirement line (no URL requirements)."""
    match = _REQ_RE.match(text.strip())
    if not match:
        raise ValueError(f"unsupported requirement: {text!r}")
    extras = match.group("extras") or ""
    extras = {e.strip() for e in extras.strip("[]").split(",") if e.strip()}
    return {
        "name": match.group("name"),
        "normalized": normalize_name(match.group("name")),
        "extras": extras,
        "specifiers": parse_specifier(match.group("spec") or ""),
        "marker": (match.group("marker") or "").strip(),
    }


# --- Environment markers ---------------------------------------------------

_COMPARE_OPS = {"==", "!=", ">=", "<=", ">", "<", "~=", "in", "not in"}


def _tokenize_marker(marker: str) -> list[tuple[str, str]]:
    tokens = []
    i = 0
    length = len(marker)
    while i < length:
        if marker[i].isspace():
            i += 1
            continue
        if marker[i] == "(" or marker[i] == ")":
            tokens.append(("paren", marker[i]))
            i += 1
            continue
        if marker[i] in "'\"":
            quote = marker[i]
            j = i + 1
            buf = []
            while j < length and marker[j] != quote:
                if marker[j] == "\\":
                    buf.append(marker[j + 1])
                    j += 2
                else:
                    buf.append(marker[j])
                    j += 1
            if j >= length:
                raise ValueError(f"unterminated marker string in {marker!r}")
            tokens.append(("string", "".join(buf)))
            i = j + 1
            continue
        # Symbolic operators, longest first so "<=" never splits into "<".
        symbolic = None
        for word in (">=", "<=", "==", "!=", "~=", ">", "<"):
            if marker.startswith(word, i):
                symbolic = word
                break
        if symbolic is not None:
            tokens.append(("op", symbolic))
            i += len(symbolic)
            continue
        # Word operators need real word boundaries so "in" never matches
        # inside an identifier such as "platform_machine". Dots are not
        # identifier characters (they belong to quoted version strings).
        word_match = re.match(r"[A-Za-z_][A-Za-z0-9_]*", marker[i:])
        if word_match:
            word = word_match.group(0)
            if word in {"and", "or", "not", "in"}:
                if word == "not":
                    tail = marker[i + len(word):]
                    stripped = tail.lstrip()
                    if stripped.startswith("in") and (
                        len(stripped) == 2
                        or not (stripped[2].isalnum() or stripped[2] == "_")
                    ):
                        tokens.append(("op", "not in"))
                        i = length - len(stripped) + 2
                        continue
                # and/or are parsed as boolean keywords via ("var", ...).
                tokens.append(
                    ("var", word) if word in {"and", "or"} else ("op", word)
                )
                i += len(word)
                continue
            tokens.append(("var", word))
            i += len(word)
            continue
        raise ValueError(f"cannot tokenize marker at {marker[i:]!r}")
    return tokens
    return tokens


class _MarkerParser:
    """Recursive-descent parser for the PEP 508 marker boolean grammar."""

    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    def peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else (None, None)

    def take(self):
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def parse(self):
        node = self.parse_or()
        if self.pos != len(self.tokens):
            raise ValueError("trailing marker tokens")
        return node

    def parse_or(self):
        node = self.parse_and()
        while self.peek() == ("var", "or"):
            self.take()
            node = ("or", node, self.parse_and())
        return node

    def parse_and(self):
        node = self.parse_unary()
        while self.peek() == ("var", "and"):
            self.take()
            node = ("and", node, self.parse_unary())
        return node

    def parse_unary(self):
        if self.peek() == ("op", "not"):
            self.take()
            return ("not", self.parse_unary())
        if self.peek() == ("paren", "("):
            self.take()
            node = self.parse_or()
            if self.take() != ("paren", ")"):
                raise ValueError("missing closing parenthesis in marker")
            return node
        return self.parse_compare()

    def parse_compare(self):
        left = self.take()
        op_kind, op = self.take()
        if op_kind != "op" or op not in _COMPARE_OPS:
            raise ValueError(f"expected marker operator, got {op!r}")
        right = self.take()
        return ("cmp", left, op, right)


def _marker_value(token, environment: dict) -> str:
    kind, value = token
    if kind == "string":
        return value
    if kind == "var":
        if value not in environment:
            raise ValueError(f"unknown marker variable {value!r}")
        return str(environment[value])
    raise ValueError(f"unexpected marker token {token!r}")


def _evaluate_node(node, environment: dict) -> bool:
    kind = node[0]
    if kind == "and":
        return _evaluate_node(node[1], environment) and _evaluate_node(
            node[2], environment
        )
    if kind == "or":
        return _evaluate_node(node[1], environment) or _evaluate_node(
            node[2], environment
        )
    if kind == "not":
        return not _evaluate_node(node[1], environment)
    _, left, op, right = node
    lv = _marker_value(left, environment)
    rv = _marker_value(right, environment)
    if op in {"in", "not in"}:
        # PEP 508: "a in b" is substring/sequence membership of a within b.
        result = lv in rv if op == "in" else lv not in rv
        return result
    if op in {"==", "!="}:
        result = lv == rv
        return result if op == "==" else not result
    # Ordering comparisons use version tuples when both look numeric.
    lt, rt = _version_tuple(lv), _version_tuple(rv)
    if op == ">=":
        return lt >= rt
    if op == "<=":
        return lt <= rt
    if op == ">":
        return lt > rt
    if op == "<":
        return lt < rt
    if op == "~=":
        return lt >= rt and lt[: len(rt) - 1] == rt[: len(rt) - 1]
    raise ValueError(f"unsupported marker operator {op}")  # pragma: no cover


def marker_environment(target) -> dict:
    """Build the PEP 508 marker environment for a target."""
    major, minor = target.cpython_minor.split(".")
    os_name = {"linux": "posix", "darwin": "posix", "windows": "nt"}[
        target.os_name
    ]
    sys_platform = {
        "linux": "linux",
        "darwin": "darwin",
        "windows": "win32",
    }[target.os_name]
    platform_system = {
        "linux": "Linux",
        "darwin": "Darwin",
        "windows": "Windows",
    }[target.os_name]
    machine_aliases = {"aarch64": "aarch64", "arm64": "arm64", "x86_64": "x86_64"}
    machine = machine_aliases.get(target.arch, target.arch)
    return {
        "python_version": target.cpython_minor,
        "python_full_version": f"{target.cpython_minor}.0",
        "implementation_name": "cpython",
        "platform_python_implementation": "CPython",
        "os_name": os_name,
        "sys_platform": sys_platform,
        "platform_system": platform_system,
        "platform_machine": machine,
        "platform_release": "",
        "platform_version": "",
        "extra": "",
    }


def marker_applies(marker: str, environment: dict) -> bool:
    """Evaluate a marker string; an empty marker always applies."""
    if not marker:
        return True
    parser = _MarkerParser(_tokenize_marker(marker))
    return _evaluate_node(parser.parse(), environment)
