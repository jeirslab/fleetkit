# Secrets and sops recipients

The kit never decrypts anything. It holds public material only: host keys,
operator recipients, and which guests may read which secret files. From that it
derives the list of recipients an estate's `.sops.yaml` needs, and a check that
the file on disk still matches.

## How a guest decrypts

A guest decrypts its sops files with its SSH host key. The host key is an
ed25519 key the guest generates on first boot; sops-nix converts it to an age
identity at activation. So a guest can read a file exactly when its host key's
age recipient is in that file's `key_groups`. `nixosModules.secrets` wires it:
sops-nix, with `sops.age.sshKeyPaths = mkDefault [ "/etc/ssh/ssh_host_ed25519_key" ]`
(a host that sets its own keeps it).

## What the model declares

- Host keys: `hostKeys.ed25519` on a guest or a site node, an
  `ssh-ed25519 AAAA...` public key (or `null` while unknown). A trailing
  comment such as `root@host` is accepted; the report drops it.
- `hostKeys.age` (same places): an `age1...` public recipient, or `null`. Either
  key is enough for a reader. Use the age recipient when adopting an existing
  `.sops.yaml`, so the recipient already in it is recorded as is. Use the ssh
  key for a new host, so there is one fact to record and the recipient is
  derived from it. When both are set the age recipient is used.
- Operators: `fleet.estates.<e>.secrets.operators.<name> = { age = "age1..."; }`
  for people or machines that are not hosts but must be able to decrypt and
  re-key.
- Files with readers: on each `secrets.files.<alias>`, `readers` is the list of
  guest or node ids that decrypt it (default none), and `operators` is the list
  of operator names (default: all of the estate's operators).

Everything here is public key material. Never put a private key or an age
identity in the model.

## The derived report

`fleet.report.sops.<estate>` carries:

- `anchors`: one entry per recipient, `kind` `host` or `operator`, with its
  `ssh` key, `age` recipient and guest or node `id` where they apply.
- `rules`: one entry per secret file, `path` and the `anchors` that may read it.
- `missingKeys`: ids of readers that have neither `hostKeys.age` nor `hostKeys.ed25519` yet. This is
  reported, not an error.

A host anchor is named by its alias in `secrets.anchors.aliases` if it has one,
otherwise `host_<guest name>` with `-` replaced by `_`, where the guest name
is the last segment of the id. Two readers of one estate with the same last
segment (`s1/web` and `s2/web`), or a reader whose anchor name is also an
operator name, fail evaluation: list one of them in `secrets.anchors.hosts`
and give it a distinct name in `secrets.anchors.aliases`.

## Rendering and checking `.sops.yaml`

From an estate repo:

```sh
nix eval --json .#fleet.report.sops.<estate> | tools/sops-config.py --report - > .sops.yaml
nix eval --json .#fleet.report.sops.<estate> | tools/sops-config.py --report - --check .sops.yaml
```

The renderer prints a `keys:` list of YAML anchors and one `creation_rules`
entry per rule, in a stable order. A host anchor with an `age` recipient uses it as is. A host with only an ssh key is converted
with `ssh-to-age` (override with `--ssh-to-age <command>`); if the command is
missing the tool fails and says so. `--check` compares the parsed structure,
not the text, and exits 1 naming the anchors or rules that differ.

## Re-keying when a host key changes

1. Read the new public key from the host (for example
   `ssh-keyscan -t ed25519 <host>`, or `/etc/ssh/ssh_host_ed25519_key.pub` on
   it) and check it out of band.
2. Put it in the model as that guest's `hostKeys.ed25519`.
3. Re-render `.sops.yaml` with the command above, and review the diff.
4. For each file whose rule names that host, run `sops updatekeys <file>`. A
   person runs this with an operator key; the kit and CI do not.
5. Commit the re-keyed files and `.sops.yaml`, then deploy.

Until step 4 is done the host cannot read the files, and a removed key can
still read the old ones: re-keying changes who can decrypt, not what an old
copy already exposed. Rotate the secret itself if the old key was compromised.

## The secrets utility

The sops files and their registry can live in a data repository of their own
(ADR-0009); the utility for it is part of the kit (ADR-0017). Nothing here
decrypts a value to the store, to stdout or to a file.

| Output | What it is |
| --- | --- |
| `lib.secrets.mkData { root; registry; sync ? {}; allow ? {}; }` | A data repository's whole flake: `root`, `fleetModule`, `report.*`, `manifests.*`, `sync`, `checks.<system>.secrets`, `devShells`, `packages`. See [registry.md](registry.md). |
| `lib.secrets.evalRegistry { registry; extraModules ? []; }` | Evaluate a registry on its own: `{ config; options; failures; ok; guard; checkReport; }`. Needs nixpkgs' lib only. |
| `lib.secrets.schemaModule` | `modules/secrets.nix`: `fleet.estates.<e>.secrets`, `fleet.sopsConfig`, `fleet.secretHosts`, `fleet.report.{sops,anchorDrift,secrets}`. Every `mkFleet` has it. |
| `lib.secrets.standaloneModule` | The stand-in for a fleet model that `evalRegistry` adds (guests and nodes from `fleet.secretHosts`). |
| `lib.secrets.sopsRef` | Resolve `sops:<estate>/<file>#<key>` to `{ name; path; key; extract; expr; }` (what the renderers use). |
| `lib.secrets.tools` | The tools' directory (stdlib Python and bash). |
| `nixosModules.secrets` | sops-nix and the SSH host key as the identity a host decrypts with. |
| `packages.<system>.secrets`, `devShells.<system>.secrets` | The `secrets` command, with sops, age, ssh-to-age, ssh and git on its PATH. |
| `templates.data` | A new data repository (`nix flake init -t github:jeirslab/fleetkit/unstable#data`). |

### The `secrets` command

Run in a data repository (or pass `--repo`).

| Command | Does |
| --- | --- |
| `secrets check` | `.sops.yaml` is what the registry renders; no plaintext; every registry key exists in its file; every reader is a recipient of its file. The same as the data repository's `nix flake check`. |
| `secrets render` | Write `.sops.yaml` from the registry. |
| `secrets scan [--all]` | The secret-pattern scan over staged changes (or every tracked file). For a pre-commit hook. |
| `secrets edit <file>` | `sops edit <file>`. Refused for protected files. |
| `secrets host add <id> <address>` | Fetch the host's ed25519 key with `ssh-keyscan`, record it in `hosts.json`, re-render `.sops.yaml`, `sops updatekeys` the files it reads. Verify the printed key out of band. |
| `secrets rekey [<file>...]` | `sops updatekeys` the files, or every sops file whose recipients differ from its rule. Protected files are skipped. |
| `secrets refs --flake <ref> [--prefix P]` | Every `sops.secrets` entry of the flake's systems names a file and a key that exist. Never decrypts. |
| `secrets import <.sops.yaml>` | Print a hand-written `.sops.yaml` as an estate's `secrets.layout`. |
| `secrets deploy-key ...` | `tools/deploy-key` ([deploy-key.md](deploy-key.md)). |
| `secrets app-bootstrap ...` | `tools/github-app-bootstrap` ([github-app-bootstrap.md](github-app-bootstrap.md)). |

`edit`, `host add` and `rekey` need an operator's age key (`SOPS_AGE_KEY_FILE`).

### Tests

`tools/gates.sh` runs them with no real secret: `tests/sops_config.sh` and
`tests/sops_config_multi.sh` (the single- and multi-estate renderer, `--check`,
`--import`), `tests/secrets_scan_selftest.sh`, `tests/deploy_key.py`,
`tests/github_app_bootstrap.py`, and `tests/secrets.sh`: the registry schema
and every assertion message (`tests/schema.nix`), the data template,
`nixosModules.secrets`, and `tests/e2e.sh` (a data repository with age and SSH
keys generated in the test and thrown away, real sops: render, encrypt, check,
every failure class, rekey, host add, protected files, allow; no value reaches
the command's output).
