# Secrets and sops recipients

The kit never decrypts anything. It holds public material only: host keys,
operator recipients, and which guests may read which secret files. From that it
derives the list of recipients an estate's `.sops.yaml` needs, and a check that
the file on disk still matches.

## How a guest decrypts

A guest decrypts its sops files with its SSH host key. The host key is an
ed25519 key the guest generates on first boot; sops-nix converts it to an age
identity at activation. So a guest can read a file exactly when its host key's
age recipient is in that file's `key_groups`. The estate adds sops-nix itself;
the kit does not wire it.

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
