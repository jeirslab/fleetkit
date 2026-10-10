"""API tokens: who a bearer token is, and what a scoped one may ask for.

The server has at most one unscoped token (FLEETKIT_API_TOKEN, everything, as
before) and any number of named, scoped ones read from a JSON file
(FLEETKIT_API_TOKENS_FILE):

  {"tokens": {"<name>": {
      "sha256": "<hex digest of the value>"  or  "file": "<path to the value>",
      "estates": [...], "hives": [...], "infra": "none|preview|apply",
      "nixos": "none|build|apply", "goals": [...], "revs": "deploy-branch|head|any",
      "allow": false}}}

The file holds no value: a digest, or the path of a file with the value. Only
digests are kept in memory, and neither a value nor a digest is ever put in a
job, an event, a log line or an error.

A scoped token is default-deny. `check` walks every field of DeployRequest:
a field this module does not classify is refused unless it is at its default,
so a field added to the request later is closed to scoped tokens until it is
classified here (cli/tests/test_api.py fails until it is).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, get_args

from .pipeline import DeployRequest

# What a job's `by` says when no scoped token started it.
UNSCOPED = "api-token"  # the single FLEETKIT_API_TOKEN
NO_AUTH = "no-auth"     # a server run with --no-auth
GITOPS = "gitops"       # the server itself: a push, a poll, a pull request
RESERVED = (UNSCOPED, NO_AUTH, GITOPS)

GOALS: tuple[str, ...] = get_args(DeployRequest.model_fields["goal"].annotation)
INFRA = ("none", "preview", "apply")
NIXOS = ("none", "build", "apply")
REVS = ("deploy-branch", "head", "any")

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_HEX = re.compile(r"[0-9a-fA-F]{64}")
_KEYS = {"sha256", "file", "estates", "hives", "infra", "nixos", "goals", "revs", "allow"}


class TokenError(Exception):
    """The tokens cannot be used as configured: the server must not start."""


@dataclass(frozen=True)
class Scope:
    estates: tuple[str, ...] = ()
    hives: tuple[str, ...] = ()
    infra: str = "none"
    nixos: str = "none"
    goals: tuple[str, ...] = ("dry-activate",)
    revs: str = "deploy-branch"
    allow: bool = False


@dataclass(frozen=True)
class Principal:
    """Who makes a request. `scope` None: everything (the unscoped token)."""
    name: str
    scope: Optional[Scope] = None

    def sees(self, estate: str) -> bool:
        return self.scope is None or estate in self.scope.estates


@dataclass(frozen=True, repr=False)
class Token:
    name: str
    digest: bytes  # sha256 of the value; the value itself is not kept
    scope: Scope

    def __repr__(self) -> str:  # never the digest
        return f"Token({self.name!r})"


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


def _strings(name: str, key: str, v: Any, among: tuple[str, ...] | None = None) -> tuple[str, ...]:
    if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
        raise TokenError(f"token {name}: {key} must be a list of names")
    if among is not None:
        bad = [x for x in v if x not in among]
        if bad:
            raise TokenError(f"token {name}: {key} has {', '.join(bad)}; one of {', '.join(among)}")
    return tuple(v)


def _one_of(name: str, key: str, v: Any, among: tuple[str, ...]) -> str:
    if not isinstance(v, str) or v not in among:
        raise TokenError(f"token {name}: {key} must be one of {', '.join(among)}")
    return v


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise TokenError(f"{k!r} is given twice")
        out[k] = v
    return out


def _entry(name: str, e: Any, base: Path) -> Token:
    if not _NAME.fullmatch(name):
        raise TokenError(f"token name {name!r}: letters, digits, '.', '_' and '-', at most 64, not starting "
                         "with punctuation")
    if name in RESERVED:
        raise TokenError(f"token name {name!r} is reserved ({', '.join(RESERVED)})")
    if not isinstance(e, dict):
        raise TokenError(f"token {name}: must be an object")
    unknown = sorted(set(e) - _KEYS)
    if unknown:
        raise TokenError(f"token {name}: unknown key {', '.join(unknown)} (keys: {', '.join(sorted(_KEYS))})")
    if ("sha256" in e) == ("file" in e):
        raise TokenError(f"token {name}: exactly one of sha256 (the digest of the value) and file (a path to "
                         "the value) is needed")
    if "sha256" in e:
        if not isinstance(e["sha256"], str) or not _HEX.fullmatch(e["sha256"]):
            raise TokenError(f"token {name}: sha256 must be 64 hex digits")
        digest = bytes.fromhex(e["sha256"])
        if hmac.compare_digest(digest, _digest("")):
            # As for an empty file: no bearer value is not a token.
            raise TokenError(f"token {name}: sha256 is the digest of the empty value")
    else:
        if not isinstance(e["file"], str) or not e["file"]:
            raise TokenError(f"token {name}: file must be a path")
        path = base / e["file"]
        try:
            value = path.read_text().strip()
        except (OSError, UnicodeDecodeError) as err:
            # The reason, not the content: a file that does not decode is not quoted.
            why = err.strerror if isinstance(err, OSError) else "not text"
            raise TokenError(f"token {name}: cannot read {path}: {why}") from None
        if not value:
            raise TokenError(f"token {name}: {path} is empty")
        digest = _digest(value)
    estates = _strings(name, "estates", e.get("estates", []))
    if not isinstance(e.get("allow", False), bool):
        raise TokenError(f"token {name}: allow must be true or false")
    return Token(name, digest, Scope(
        estates=estates,
        hives=_strings(name, "hives", e["hives"]) if "hives" in e else estates,
        infra=_one_of(name, "infra", e.get("infra", "none"), INFRA),
        nixos=_one_of(name, "nixos", e.get("nixos", "none"), NIXOS),
        goals=_strings(name, "goals", e.get("goals", ["dry-activate"]), GOALS),
        revs=_one_of(name, "revs", e.get("revs", "deploy-branch"), REVS),
        allow=e.get("allow", False),
    ))


def load(path: str | Path) -> list[Token]:
    """The scoped tokens of a tokens file. Anything wrong with the file, or
    with a file it names, is a TokenError: the server refuses to start rather
    than run with fewer tokens than were configured."""
    path = Path(path)
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError) as err:
        why = err.strerror if isinstance(err, OSError) else "not text"
        raise TokenError(f"tokens file {path}: cannot read: {why}") from None
    try:
        doc = json.loads(text, object_pairs_hook=_no_duplicate_keys)
        if not isinstance(doc, dict) or set(doc) != {"tokens"} or not isinstance(doc["tokens"], dict):
            raise TokenError('must be {"tokens": {"<name>": {...}}}')
        out = [_entry(name, e, path.parent) for name, e in doc["tokens"].items()]
    except json.JSONDecodeError as err:
        # Position only: err.doc is the file.
        raise TokenError(f"tokens file {path}: not JSON (line {err.lineno}, column {err.colno})") from None
    except TokenError as err:
        raise TokenError(f"tokens file {path}: {err}") from None
    return out


class Authenticator:
    """Bearer header -> Principal. Every configured digest is compared, in
    constant time each and with no early exit, against the digest of what was
    presented: the time taken does not depend on which token matched, or on
    whether one did."""

    def __init__(self, single: Optional[str], scoped: Optional[list[Token]] = None):
        self._entries: list[tuple[bytes, Principal]] = []
        if single is not None:
            self._entries.append((_digest(single), Principal(UNSCOPED, None)))
        for t in scoped or []:
            self._entries.append((t.digest, Principal(t.name, t.scope)))
        names = [p.name for _, p in self._entries]
        twice = sorted({n for n in names if names.count(n) > 1})
        if twice:
            raise TokenError(f"token name given twice: {', '.join(twice)}")
        for i, (d, p) in enumerate(self._entries):
            for d2, p2 in self._entries[i + 1:]:
                if hmac.compare_digest(d, d2):
                    # Two names for one value: which scope a request has would
                    # depend on the order of the file.
                    raise TokenError(f"tokens {p.name} and {p2.name} have the same value")
        # A scoped value read from a file is stripped; the unscoped one is used
        # as it was given. The same text for both, apart from whitespace around
        # it, would pass the check above and leave the unscoped token matching
        # nothing a client sends.
        if single is not None and single.strip() != single:
            bare = _digest(single.strip())
            for d, p in self._entries[1:]:
                if hmac.compare_digest(d, bare):
                    raise TokenError(f"tokens {UNSCOPED} and {p.name} have the same value")

    @property
    def open(self) -> bool:
        """No token at all: --no-auth."""
        return not self._entries

    def authenticate(self, header: str) -> Optional[Principal]:
        if self.open:
            return Principal(NO_AUTH, None)
        bearer = header.startswith("Bearer ")
        got = _digest(header[len("Bearer "):] if bearer else "")
        found: Optional[Principal] = None
        for digest, principal in self._entries:
            if hmac.compare_digest(digest, got) & bearer:
                found = principal
        return found


# ── what a scoped token may ask for ──────────────────────────────────────


class Refused(Exception):
    """A request outside the token's scope: `field` is what was refused."""

    def __init__(self, field: Optional[str], why: str):
        super().__init__(why)
        self.field, self.why = field, why


def _default(req: DeployRequest, name: str) -> Any:
    """The field's default (a required field has none: nothing equals it)."""
    return type(req).model_fields[name].get_default(call_default_factory=True)


Check = Callable[[Scope, DeployRequest, str], Optional[str]]


def _estate(sc: Scope, req: DeployRequest, name: str) -> Optional[str]:
    if req.estate not in sc.estates:
        return f"estate {req.estate!r} is not one of the token's estates"
    return None


def _hive(sc: Scope, req: DeployRequest, name: str) -> Optional[str]:
    # What pipeline.run deploys: the named hive, else the estate's.
    if req.hive is None and not req.nixos:
        return None
    hive = req.hive or req.estate
    if hive not in sc.hives:
        return f"hive {hive!r} is not one of the token's hives"
    return None


def _infra(sc: Scope, req: DeployRequest, name: str) -> Optional[str]:
    if not req.infra:
        return None
    if sc.infra == "none":
        return "the token may not run the Pulumi stage (send infra: false)"
    if sc.infra == "preview" and not req.preview:
        return "the token may run the Pulumi stage as a preview only"
    return None


def _nixos(sc: Scope, req: DeployRequest, name: str) -> Optional[str]:
    if not req.nixos:
        return None
    if sc.nixos == "none":
        return "the token may not run the Colmena stage (send nixos: false)"
    if sc.nixos == "build" and not req.preview:
        return "the token may run the Colmena stage as a preview (a build) only"
    return None


def _goal(sc: Scope, req: DeployRequest, name: str) -> Optional[str]:
    if req.goal in sc.goals:
        return None
    # A request that applies nothing with Colmena does not use its goal: the
    # default may stay; any other value is still refused.
    if not (req.nixos and not req.preview) and req.goal == _default(req, name):
        return None
    return f"goal {req.goal!r} is not one of the token's goals ({', '.join(sc.goals) or 'none'})"


def _stacks(sc: Scope, req: DeployRequest, name: str) -> Optional[str]:
    # Stacks of the request's estate only (render.stacks_of), which was checked.
    if req.stacks is not None and sc.infra == "none":
        return "the token may not run the Pulumi stage, so it may not name stacks"
    return None


def _on(sc: Scope, req: DeployRequest, name: str) -> Optional[str]:
    # Nodes of the request's hive only, which was checked.
    if req.on and sc.nixos == "none":
        return "the token may not run the Colmena stage, so it may not name nodes"
    return None


def _free(sc: Scope, req: DeployRequest, name: str) -> Optional[str]:
    return None


def _allow(sc: Scope, req: DeployRequest, name: str) -> Optional[str]:
    if not sc.allow and getattr(req, name) != _default(req, name):
        return f"the token may not set {name}"
    return None


# Every field of DeployRequest but `rev`, which needs the repository (check).
_CHECKS: dict[str, Check] = {
    "estate": _estate,
    "hive": _hive,
    "infra": _infra,
    "nixos": _nixos,
    "goal": _goal,
    "stacks": _stacks,
    "on": _on,
    # Asks for less: nothing changes.
    "preview": _free,
    # A label on the job (which pull request it belongs to); the server
    # reports to GitHub only for jobs it started itself.
    "pr": _free,
    "refresh": _allow,
    "targets": _allow,
    "allow_replace": _allow,
    "allow_delete": _allow,
    "allow_update": _allow,
    "allow_create": _allow,
}
CLASSIFIED = frozenset(_CHECKS) | {"rev"}

# pin(rev, heads=, preview=) -> the sha when rev (None: the deploy branch head)
# is a commit the scope's `revs` allows, else None (api.py, over
# gitops.Repo.commit_on). heads False (revs = deploy-branch): a commit on the
# deploy branch or a preview branch. heads True (revs = head): the head of the
# deploy branch, or in a preview the head of a preview branch too.
Pin = Callable[..., Optional[str]]


def check(p: Principal, req: DeployRequest, pin: Optional[Pin] = None) -> DeployRequest:
    """-> the request to run, or raises Refused. An unscoped principal's
    request is returned as it is. A scoped one's is checked field by field,
    and its rev is replaced by the commit that was checked, so that a branch
    moving afterwards cannot change what runs."""
    sc = p.scope
    if sc is None:
        return req
    for name in type(req).model_fields:
        if name == "rev":
            continue
        fn = _CHECKS.get(name)
        if fn is not None:
            why = fn(sc, req, name)
        else:
            # Not classified: closed, unless the request leaves it alone.
            why = None if getattr(req, name) == _default(req, name) else f"a scoped token may not set {name}"
        if why:
            raise Refused(name, why)
    if not req.infra and not req.nixos:
        # Every field is within the scope, and nothing would run: but the job
        # would hold the estate's one deploy slot, and fetch and check out a
        # commit. Refused before the repository is asked.
        raise Refused("nixos", "the request runs no stage (infra and nixos are both false): a scoped token "
                               "starts only a job that runs a stage")
    if sc.revs == "any":
        return req
    if pin is None:
        # No repo: the server deploys its working tree, and refuses a rev anyway.
        if req.rev is not None:
            raise Refused("rev", "this server has no repo: the token may not name a rev")
        return req
    heads = sc.revs == "head"
    sha = pin(req.rev, heads=heads, preview=req.preview)
    if sha is None:
        raise Refused("rev", "rev is not the head of the deploy branch (or, in a preview, of a preview branch)"
                      if heads else "rev is not a commit on the deploy branch or a preview branch")
    return req.model_copy(update={"rev": sha})
