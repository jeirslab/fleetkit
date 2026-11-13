# Secrets data repository

The sops-encrypted files of a fleet and their registry, built with
fleetkit's secrets utility ([docs/registry.md](https://github.com/jeirslab/fleetkit/blob/unstable/docs/registry.md)).
The flake is one `fleetkit.lib.secrets.mkData` call.

- `registry.nix`: every file, the key names it holds, its readers (hosts),
  its operators, and where a key is mirrored (Infisical, Bitwarden).
- `hosts.json`: the hosts' public keys (`secrets host add` writes it).
- `.sops.yaml`: rendered from the registry (`secrets render`); never edited.
- `secrets/`: the sops files.

```sh
nix develop                     # the secrets CLI, sops, age, ssh-to-age
secrets render                  # .sops.yaml from the registry
secrets edit secrets/app.yaml   # sops edit
secrets host add example/app 192.0.2.10   # record a host key, re-key what it reads
secrets check                   # also run by `nix flake check`
secrets scan                    # before a commit: no plaintext staged
```

A consumer (a fleet) takes this repository as a flake input and uses its
`root` (the files' store path) and `fleetModule` (the registry).
