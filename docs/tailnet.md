# Tailnet client versions

`fleetkit tailnet versions` lists the Tailscale client version of every node of the tailnets named in
the repository's `fleetkit.toml`, by asking the control servers (no login to the nodes).

```
fleetkit tailnet versions [--format table|markdown|json] [--summary-file F] [--json-file F]
                          [--only NAME] [--fleet-host H ...]
                          [--fail-on-stale-fleet] [--set-auto-update-default]
```

- Exit 0 whatever the report says. `--fail-on-stale-fleet` exits 1 when a NixOS host of the fleet is
  behind. `--summary-file` appends the Markdown report (`$GITHUB_STEP_SUMMARY`).
- "Latest" is the tailscale of a package flake (`[tailnets_latest]`: `repo`, `ref`, `paths`; first
  existing of `pkgs/tailscale/version.json`, `source.json`, read from GitHub raw). Upstream's latest
  release is shown beside it.
- A fleet NixOS host is a node whose short name is a `nixosConfigurations` name, or its last part
  (`homelab-lan-dumpy` is `dumpy`). `--fleet-host` replaces the `nix eval` of the flake.
- `kind = "tailscale"`: `GET /api/v2/tailnet/-/devices?fields=all` (`clientVersion`, `os`) and
  `GET /api/v2/tailnet/-/settings` (`devicesAutoUpdatesOn`, the tailnet-wide default; the API has no
  per-device setting). `--set-auto-update-default` sends `PATCH /api/v2/tailnet/-/settings
  {"devicesAutoUpdatesOn": true}` when it is off. NixOS hosts cannot self-update; they move with the
  input bump and a deploy.
- `kind = "headscale"`: `GET /api/v1/node`. Headscale (checked v0.28) does not return the client
  version, so its nodes show `unknown` and are never stale; a server that sends `hostinfo.IPNVersion`
  or `clientVersion` is read.

```toml
secrets_root = "../fleet-secrets"        # FLEETKIT_SECRETS_ROOT overrides

[[tailnets]]
name = "homelab"
kind = "tailscale"
secret = { env = "TAILSCALE_API_KEY", file = "site/homelab-lan/generated.yaml", key = "integrations/tailscale/api_key" }

[[tailnets]]
name = "xgcs"
kind = "headscale"
url = "https://vpn.xgcs.xyz"
secret = { env = "HEADSCALE_VPN_API_KEY", file = "xgcs/secrets/integrations.yaml", key = "integrations/headscale/vpn/api_key" }
```

The key is the environment variable when it is set, else `sops -d --extract` of the file under
`secrets_root` (else the repository). It is never printed.
