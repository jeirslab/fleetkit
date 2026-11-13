# The registry and a data repository

A data repository holds the sops files of one or more estates and the registry
that describes them. Its flake is one call:

```nix
{
  inputs.fleetkit.url = "github:jeirslab/fleetkit/unstable";
  outputs = { self, fleetkit, ... }: fleetkit.lib.secrets.mkData {
    root = self;                 # the files (.sops.yaml, the sops files)
    registry = ./registry.nix;   # a module, below
    sync = { infisical.enable = false; bitwarden.enable = false; };
    allow = { missingKeys = [ ]; recipients = [ ]; };
  };
}
```

## What `mkData` gives

| Output | What it is |
| --- | --- |
| `root` | The files' store path. A consumer reads a secret at `root + "/<path>"`. |
| `fleetModule` | The registry as a module for a fleet model (fleetkit's `mkFleet`), which brings the schema itself. There the model's guests and nodes, and their `hostKeys`, are the readers. |
| `report.sops` | The multi-estate `.sops.yaml` renderer's input (`tools/sops-config.py --config`). |
| `report.estates` | Per estate, `{ anchors; rules; missingKeys; }`: the shape `fleet.report.sops.<estate>` has always had (`tools/sops-config.py --report`). |
| `report.files`, `report.placements`, `report.anchorDrift` | Every file with its keys and readers; the Infisical and Bitwarden placements; anchor drift. |
| `report.check` | What `secrets check` reads. |
| `manifests.infisical`, `manifests.bitwarden` | The placements as nixfisical and nixwarden manifest entries, `sopsFile` under `root`. |
| `sync` | `{ infisical.enable; bitwarden.enable; }`, for a workflow that syncs on push. |
| `checks.<system>.secrets` | `secrets check` over `root`. Evaluating it also fails on any registry assertion. |
| `devShells`, `packages` | The `secrets` CLI. |

`allow` excuses known drift, so the check can be on while it is fixed:
`missingKeys` takes `"<path>#<key>"`, `recipients` takes `"<path>"`. Excused
items are listed as such. A file the registry names that does not exist is excused
when every key it declares is.

## The registry

The registry is a NixOS-style module. It sets:

- `fleet.estates.<estate>.secrets`: `backend`, `operators`, `files`,
  `anchors`, `recipients` (see [secrets.md](secrets.md)), plus `placements` on
  each file and `layout` on the estate (below).
- `fleet.secretHosts."<estate>/<name>"` = `{ ed25519 ? null; age ? null; kind ? "guest"; }`:
  the host keys the readers name; `kind = "node"` for `"<site>/<name>"`. The
  template reads them from `hosts.json`, which `secrets host add` writes.
  On its own (`lib.evalRegistry`) they are the guests
  (`config.fleet.guests.<estate>.<name>.id`) and nodes
  (`config.fleet.sites.<site>.nodes.<name>.id`) a registry refers to. In a
  fleet model the model's own `hostKeys` are used, and
  `fleet.report.secrets.hostDrift` lists the ids whose keys differ.
- `fleet.sopsConfig`: `header` (the comment at the top of `.sops.yaml`),
  `estates` (which estates, in what order), `protect` (regexes of paths the CLI
  never re-encrypts: files that move only as bytes).

### Placements

Each declared key can say where its value is mirrored:

```nix
files.app = {
  path = "secrets/app.yaml";
  keys = [ "db/password" "admin/user" "admin/password" ];
  readers = [ config.fleet.guests.example.app.id ];
  placements = {
    "db/password".infisical = { project = "app"; folder = "/db"; groups = [ "developers" ]; };
    "admin/password".bitwarden = {
      org = "example"; collection = "apps"; name = "App admin";
      username = { sopsKey = "admin/user"; };   # another key of this file
      uris = [ "https://app.example.org" ];
    };
  };
};
```

The fields are those of nixfisical's `lib.mkInfisical` (`project`, `folder ? "/"`,
`environment ? "prod"`, `name ? null`, `groups ? [ ]`, `source ? "sops"`) and of
nixwarden's `lib.mkBitwarden` (`org`, `collection`, `type ? "login"`,
`field ? null`, `name ? null`, `username`, `uris`, `totp`, `notes`, `fields`,
`favorite`, `reprompt`), so the result of either call can be assigned as is. A
Bitwarden slot is a literal string or `{ sopsKey; sopsFile ? null; }`, where
`sopsFile` is the alias of another file of the same estate.

Evaluation fails when a placement names an undeclared key, a folder is not
absolute, an environment is not a slug, a Bitwarden field is not a slot of its
item type, a login-only slot is set on another type, a reference names an
undeclared key, or two keys land on the same Infisical destination
(`project/environment/folder:name`) or the same Bitwarden item
(`org/collection:name`) anywhere in the registry.

### A verbatim layout

An estate whose `.sops.yaml` was written by hand keeps its anchors and rules as
they are, in their order:

```nix
fleet.estates.legacy.secrets = {
  backend = "sops";
  layout = {
    prefix = "legacy_";     # every anchor of the estate gets it
    root = "legacy";        # its rules are re-rooted under legacy/
    keysComment = "legacy/.sops.yaml, anchors prefixed legacy_";
    rulesComment = "legacy/.sops.yaml, in its order, re-rooted under legacy/";
    keys = [ { name = "master"; age = "age1..."; } ];
    rules = [ { pathRegex = "secrets/vpn\\.yaml$"; anchors = [ "master" ]; } ];
  };
};
```

`secrets import <.sops.yaml> --estate legacy --prefix legacy_ --root legacy`
prints that block from the file. Re-rooting: an anchored `^x` becomes
`^<root>/x`, an unanchored `x` becomes `^<root>/.*x` (it matched anywhere below
the old repository's root).

### The multi-estate `.sops.yaml`

`secrets render` writes, in this order: the header (`# ` per line), `keys:`
with each estate's derived anchors (sorted, as the single-estate renderer does)
then its verbatim keys, then `creation_rules:` with each estate's derived rules
(sorted by path) then its verbatim rules. An estate's comments come before its
part of each list. Anchor names that clash after the prefix fail evaluation.
`secrets check` requires the file to be exactly that text.

## The checks

`secrets check` and the data repository's `nix flake check`:

1. `sops-config`: `.sops.yaml` is what the registry renders.
2. `scan`: no plaintext secret in any file (`tools/hooks/secrets-scan.sh --all`).
3. `keys`: every registry file exists and holds every declared key. A sops
   file's key names are in clear; its values are never read.
4. `recipients`: every sops file a creation rule matches (the first match, as
   sops picks it) is encrypted to every recipient of the rule. A recipient
   beyond the rule (stale) and a sops file no rule matches are warnings.

Placements (unique, complete, referring to declared keys) are checked when the
registry evaluates.
