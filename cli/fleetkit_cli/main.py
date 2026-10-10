"""fleetkit command line.

  fleetkit estates                       estates and their Pulumi stacks
  fleetkit render ESTATE [STACK]         write the Pulumi projects, print where
  fleetkit preview ESTATE [options]      pulumi preview + colmena build
  fleetkit deploy ESTATE [options]       pulumi up + colmena apply
  fleetkit adopt ESTATE [options]        import what already exists into state
  fleetkit serve [--listen ...]          the same deploys over HTTP (api.py)

Run from the estate repo (or pass --flake / set FLEETKIT_FLAKE). Pulumi state
goes to PULUMI_BACKEND_URL, which must be set; PULUMI_CONFIG_PASSPHRASE(_FILE)
encrypts its secrets; SOPS_AGE_KEY_FILE decrypts the model's.
"""
from __future__ import annotations

import contextlib
import json
import os
import signal
import sys
from pathlib import Path

import click

from . import adopt as adoption
from . import guard, pipeline, render
from .events import Cancelled, Emitter
from .settings import Settings, SettingsError


def _settings(ctx: click.Context) -> Settings:
    try:
        return Settings.from_env(ctx.obj["flake"], ctx.obj["state_dir"])
    except SettingsError as e:
        raise click.ClickException(str(e))


@contextlib.contextmanager
def _signals(ev: Emitter):
    """SIGINT, SIGTERM and SIGHUP become a cancel of the run instead of
    killing the process where it stands: the engine (started in its own
    session, infra.OwnedPulumi) is told to stop after the step in flight, the
    run ends through its `finally` blocks (the temporary program of an
    adoption is removed, the run's directory too), and what was done is
    reported. A second signal makes the engine terminate at once."""
    def handler(signum, frame):  # noqa: ARG001
        click.secho(f"{signal.Signals(signum).name}: stopping (again: at once)", fg="red", err=True)
        ev.cancel()

    old = {}
    try:
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            old[sig] = signal.signal(sig, handler)
    except ValueError:  # not the main thread: the caller owns the signals
        pass
    try:
        yield
    finally:
        for sig, h in old.items():
            signal.signal(sig, h)


def _cancelled(e: Exception) -> click.ClickException:
    text = str(e)
    return click.ClickException("cancelled" if text in ("", "cancelled") else f"cancelled: {text}")


def _printer(as_json: bool):
    def sink(e: dict) -> None:
        if as_json:
            click.echo(json.dumps(e))
            return
        if e["kind"] == "log":
            click.echo(f"  {e['stage']:>6} | {e['line']}")
        elif e["kind"] == "plan" and e["stage"] == "infra":
            # Every resource that is not `same`, by op: read this, not the counts.
            for line in e["text"]:
                click.secho(line, fg="red" if "refuse" in line.lower() else "yellow", err=True)
        else:
            rest = {k: v for k, v in e.items() if k not in ("ts", "stage", "kind", "trace")}
            click.secho(f"[{e['stage']}] {e['kind']} {json.dumps(rest) if rest else ''}",
                        fg="red" if e["kind"] in ("error", "failed-step") else "cyan", err=True)
    return sink


@click.group()
@click.option("--flake", envvar="FLEETKIT_FLAKE", help="The estate repo (default: cwd).")
@click.option("--state-dir", envvar="FLEETKIT_STATE_DIR",
              help="Work dirs and job logs (default: <flake>/.fleet/fleetkit).")
@click.pass_context
def cli(ctx: click.Context, flake: str | None, state_dir: str | None) -> None:
    ctx.obj = {"flake": flake, "state_dir": state_dir}


@cli.command()
@click.pass_context
def estates(ctx: click.Context) -> None:
    """Estates and their Pulumi stacks."""
    s = _settings(ctx)
    for e, stacks in sorted(render.estates(s).items()):
        click.echo(f"{e}: {' '.join(stacks)}")


@cli.command("render")
@click.argument("estate")
@click.argument("stack", required=False)
@click.pass_context
def render_cmd(ctx: click.Context, estate: str, stack: str | None) -> None:
    """Write the Pulumi project of each stack and print its directory.

    The directory is this command's own (<state>/runs/...): no deploy reads
    it. It is the program a deploy would run, every guest protected, and is
    removed by a later run once it is a day old."""
    s = _settings(ctx)
    ev = Emitter(lambda e: None)
    run = render.Run(s, "render", keep=True)
    for name, st in render.stacks_of(s, estate, [stack] if stack else None).items():
        project = render.render(s, name, st, ev, run)
        click.echo(f"{project.wd}  (program {st['file']} sha256 {project.sha256})")


def _deploy_options(f):
    for opt in reversed([
        click.option("--stack", "stacks", multiple=True, help="Pulumi stack (repeat; default all)."),
        click.option("--no-infra", is_flag=True, help="Skip Pulumi."),
        click.option("--no-nixos", is_flag=True, help="Skip Colmena."),
        click.option("--hive", help="Hive under hives.* (default: the estate)."),
        click.option("--on", multiple=True, help="Colmena --on (node or @tag; repeat)."),
        click.option("--refresh", is_flag=True, help="Refresh Pulumi state first."),
        click.option("--target", "targets", multiple=True, help="Pulumi --target URN (repeat)."),
        click.option("--allow-replace", multiple=True, metavar="[STACK/]KEY",
                     help="A guest this deploy may replace: destroy and recreate (repeat). STACK/KEY, a URN, or a KEY "
                          "that only one stack of the run has."),
        click.option("--allow-delete", multiple=True, metavar="[STACK/]KEY",
                     help="A guest this deploy may delete (repeat). STACK/KEY, a URN, or a KEY "
                          "that only one stack of the run has."),
        click.option("--allow-update", multiple=True, metavar="[STACK/]KEY",
                     help="A guest this deploy may update in place: a reboot (repeat). STACK/KEY, a URN, or a KEY "
                          "that only one stack of the run has."),
        click.option("--allow-create", multiple=True, metavar="[STACK/]KEY",
                     help="A guest this deploy may create although the stack declares it as already existing "
                          "(it has an adoption id and is not in state), or an HA resource it may create "
                          "(repeat). STACK/KEY, a URN, or a KEY that only one stack of the run has."),
        click.option("--json", "as_json", is_flag=True, help="Events as JSON lines on stdout."),
    ]):
        f = opt(f)
    return f


def _run(ctx: click.Context, estate: str, preview: bool, goal: str, kw: dict) -> None:
    s = _settings(ctx)
    req = pipeline.DeployRequest(
        estate=estate, stacks=list(kw["stacks"]) or None, infra=not kw["no_infra"],
        nixos=not kw["no_nixos"], hive=kw["hive"], on=list(kw["on"]), goal=goal,
        preview=preview, refresh=kw["refresh"], targets=list(kw["targets"]),
        allow_replace=list(kw["allow_replace"]), allow_delete=list(kw["allow_delete"]),
        allow_update=list(kw["allow_update"]), allow_create=list(kw["allow_create"]))
    ev = Emitter(_printer(kw["as_json"]))
    try:
        with _signals(ev):
            result = pipeline.run(s, req, ev)
    except Cancelled as e:
        if kw["as_json"] and e.result is not None:
            click.echo(json.dumps({"cancelled": True, **ev.redact(e.result)}))
        raise _cancelled(e)
    except KeyboardInterrupt:
        ev.cancel()
        raise click.ClickException("interrupted")
    except guard.GuardError as e:
        if kw["as_json"] and e.result is not None:
            click.echo(json.dumps({"refused": True, **ev.redact(e.result)}))
        raise click.ClickException(ev.redact(str(e)))
    except Exception as e:  # noqa: BLE001 - shown as the command's error
        if ev.cancelled:
            raise click.ClickException(ev.redact(f"cancelled ({type(e).__name__}: {e})"))
        text = str(e) if type(e).__name__ == "EngineError" else f"{type(e).__name__}: {e}"
        raise click.ClickException(ev.redact(text))
    click.secho(json.dumps(result), fg="green", err=not kw["as_json"])


@cli.command()
@click.argument("estate")
@_deploy_options
@click.pass_context
def preview(ctx: click.Context, estate: str, **kw) -> None:
    """pulumi preview of each stack, then colmena build. Changes nothing.

    Lists every resource that would change, by op, and which of them a deploy
    would refuse without --allow-replace / --allow-delete / --allow-update /
    --allow-create."""
    _run(ctx, estate, True, "switch", kw)


@cli.command()
@click.argument("estate")
@click.option("--goal", type=click.Choice(["switch", "test", "boot", "dry-activate"]), default="switch")
@_deploy_options
@click.pass_context
def deploy(ctx: click.Context, estate: str, goal: str, **kw) -> None:
    """pulumi up of each stack, then colmena apply GOAL.

    Every stack is previewed first. A plan that replaces, deletes or updates a
    guest (a Proxmox container or VM) is refused, with nothing applied, unless
    the guest is named: --allow-replace KEY, --allow-delete KEY,
    --allow-update KEY (an in-place update reboots a container). So is the
    create of a guest the stack declares as already existing (it has an
    adoption id and is not in state: adopt it, or --allow-create KEY). KEY is
    STACK/KEY, a URN, or a key only one stack of the run has. Each up is bound
    to the plan of its preview (the engine refuses anything else), and every
    guest that is not named is protected for the run. Ctrl-C stops the engine
    after the step in flight; again, at once."""
    _run(ctx, estate, False, goal, kw)


@cli.command("adopt")
@click.argument("estate")
@click.option("--stack", "stacks", multiple=True, help="Pulumi stack (repeat; default all).")
@click.option("--resource", "resources", multiple=True, metavar="KEY",
              help="Only these resources (repeat; default every resource with an adoption id but secrets, "
                   "which are adopted only when named here).")
@click.option("--id", "ids", multiple=True, metavar="KEY=ID",
              help="The provider's import id of a resource (repeat); overrides the stack's adoptIds.")
@click.option("--accept-update", multiple=True, metavar="KEY",
              help="Adopt KEY although its declaration differs, or the import does not record some of its "
                   "properties: it is updated in place (a guest REBOOTS).")
@click.option("--accept-absent", multiple=True, metavar="KEY",
              help="With --apply: the guest KEY, for which nothing was found by its id, really does not exist "
                   "(repeat). Without it an absent guest refuses the apply: one that exists under another id "
                   "would be created a second time by the next deploy.")
@click.option("--apply", "apply_", is_flag=True, help="Adopt. Without it: the report only, nothing changes.")
@click.option("--json", "as_json", is_flag=True, help="The report as one JSON document on stdout.")
@click.pass_context
def adopt_cmd(ctx: click.Context, estate: str, stacks: tuple[str, ...], resources: tuple[str, ...],
              ids: tuple[str, ...], accept_update: tuple[str, ...], accept_absent: tuple[str, ...], apply_: bool,
              as_json: bool) -> None:
    """Import resources that already exist into a stack's state.

    For each resource with an adoption id that is not in state yet: `import`
    (the declaration equals the live resource), `import+update` with what
    differs (live and declared), `import+unrecorded` (the import does not
    record some properties: still an update, a reboot of a guest),
    `import+local` (a guest that differs only in its provider-local timeouts:
    nothing is sent to the hypervisor, no reboot, nothing to accept), `absent`
    (it does not exist: a deploy will create it), `secret` (not adopted unless
    named with --resource), `duplicate` (its id is in state under another
    name, in any stack of the estate, or twice in this run: refused). --apply
    refuses an update that is not accepted with --accept-update KEY, an
    absent guest that is not accepted with --accept-absent KEY, and any plan
    that creates, replaces or deletes. The import id is only ever in a
    temporary program, never in the real one."""
    s = _settings(ctx)
    pairs: dict[str, str] = {}
    for pair in ids:
        key, sep, rid = pair.partition("=")
        if not sep or not key or not rid:
            raise click.ClickException(f"--id takes KEY=ID, not {pair!r}")
        pairs[key] = rid

    def sink(e: dict) -> None:  # stdout carries the report only
        if e["kind"] == "log":
            click.echo(f"  {e['stage']:>6} | {e['line']}", err=True)
        elif e["kind"] in ("diagnostic", "error", "failed-step"):
            rest = {k: v for k, v in e.items() if k not in ("ts", "stage", "kind", "trace")}
            click.secho(f"[{e['stage']}] {e['kind']} {json.dumps(rest)}", fg="red", err=True)

    ev = Emitter(sink)
    try:
        with _signals(ev):
            report = adoption.run(s, estate, ev, list(stacks) or None, list(resources), pairs,
                                  list(accept_update), apply_, list(accept_absent))
    except Cancelled as e:
        raise _cancelled(e)
    except KeyboardInterrupt:
        ev.cancel()
        raise click.ClickException("interrupted")
    except Exception as e:  # noqa: BLE001 - shown as the command's error
        if ev.cancelled:
            raise click.ClickException(ev.redact(f"cancelled ({type(e).__name__}: {e})"))
        raise click.ClickException(ev.redact(f"{type(e).__name__}: {e}"))
    if as_json:
        click.echo(json.dumps(report, indent=1))
    else:
        for line in adoption.text(report):
            click.echo(line)
    if not report["ok"]:
        ctx.exit(1)


@cli.command()
@click.option("--repo", envvar="FLEETKIT_REPO",
              help="GitOps: the estate repo's git URL; each deploy runs from a checkout of its commit.")
@click.option("--listen", default="127.0.0.1:8740", show_default=True, help="host:port")
@click.option("--token-file", envvar="FLEETKIT_API_TOKEN_FILE", help="File holding the bearer token.")
@click.option("--tokens-file", envvar="FLEETKIT_API_TOKENS_FILE",
              help="JSON file of named, scoped tokens (tokens.py; docs/pulumi.md, \"HTTP API\").")
@click.option("--no-auth", is_flag=True, help="No token; allowed only on a loopback address.")
@click.option("--workers", default=4, show_default=True, help="Estates deploying at once.")
@click.pass_context
def serve(ctx: click.Context, repo: str | None, listen: str, token_file: str | None,
          tokens_file: str | None, no_auth: bool, workers: int) -> None:
    """Serve deploys over HTTP (see `fleetkit serve --help` and api.py)."""
    import uvicorn

    from . import tokens
    from .api import create_app
    from .jobs import JobManager

    host, _, port = listen.rpartition(":")
    token = os.environ.get("FLEETKIT_API_TOKEN")
    if token_file:
        token = Path(token_file).read_text().strip()
    if token_file and not token and not no_auth:
        raise click.ClickException(f"the token file {token_file} is empty")
    if no_auth:
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise click.ClickException("--no-auth is only allowed on a loopback address")
        if tokens_file:
            raise click.ClickException("--no-auth and a tokens file (FLEETKIT_API_TOKENS_FILE): without auth "
                                       "every request may do everything, so the scopes would mean nothing")
        token = None
    token = token or None
    # A tokens file that cannot be used as written stops the server here: it
    # never runs with fewer tokens than were configured.
    try:
        scoped = tokens.load(tokens_file) if tokens_file else []
        tokens.Authenticator(token, scoped)
    except tokens.TokenError as e:
        raise click.ClickException(str(e))
    if not no_auth and not token and not scoped:
        raise click.ClickException("no token: set FLEETKIT_API_TOKEN(_FILE) or pass --token-file, or name "
                                   "scoped tokens in FLEETKIT_API_TOKENS_FILE")
    try:
        s = Settings.from_env(ctx.obj["flake"], ctx.obj["state_dir"], repo)
    except SettingsError as e:
        raise click.ClickException(str(e))
    gitops = None
    if s.git is None:
        def runner(req, ev):
            if req.rev:
                raise ValueError("rev needs a server with a repo (--repo / FLEETKIT_REPO)")
            return pipeline.run(s, req, ev)
        manager = JobManager(s.state_dir, runner, workers)
    else:
        from .gitops import GitOps, Repo, make_runner

        git = Repo(s)

        runner = make_runner(s, git)

        manager = JobManager(s.state_dir, runner, workers)
        gitops = GitOps(s, git, manager)
        gitops.start_polling()
    uvicorn.run(create_app(manager, s, token, gitops, scoped), host=host or "127.0.0.1", port=int(port),
                log_level="info")


def main() -> None:
    cli(prog_name="fleetkit")


if __name__ == "__main__":
    sys.exit(main())
