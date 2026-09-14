# AGENTS.md — operating manual for AI agents

This file is the contract for any AI agent working **on** fleetkit or **with**
fleetkit from a consumer repo. It is deliberately short: the real API is
machine-discoverable (see "Discovering the API" below).

## What this is

A parameterized NixOS fleet framework: fleet manifest schema (`fleet.*`
options), terranix emitters (Proxmox VE, Xen Orchestra, Cloudflare, Grafana),
colmena deploy integration (`lib.mkFleet`), an operator CLI (`fleet`), NixOS
service modules (`infra.*` options), an ansible layer for non-NixOS guests
and hypervisor management, and image builders. Consumers import it as a flake
input and describe their fleet as data.

## Iron rules

1. **The `fleet` CLI is the only sanctioned path** for infrastructure
   operations — never run raw `tofu`, `terraform`, or `colmena`. The CLI owns
   secret decryption, backend auth, inventory generation, and runtime env
   (ansible paths, plugin caches). If an operation you need is missing, add a
   CLI verb; do not bypass.
2. **`nix flake check` is the acceptance gate** — seven checks: `example-fleet`
   (parameter-surface completeness against the template), `example-tf-render`
   (every example stack renders valid Terraform JSON), `mail-internal`
   (`infra.mail.internal` instantiates and its rendered main.cf still refuses
   to relay), `launcher` (CLI builds and runs), `docs` (options docs build with
   `warningsAreErrors`), `ansible-syntax` (framework playbooks parse), and
   `compute-surface-golden` (emitters render byte-identically). All seven must
   pass before any change is done.
3. **Every option carries a description** — the `docs` check fails otherwise.
   Descriptions are static strings; never interpolate config values into them.
4. **No site-specific literals in framework code.** Site values live behind
   `fleet.settings.*` / `fleet.network.*` (Nix, projected to the CLI catalog), or
   ansible variables named after their settings counterparts. No company or
   personal defaults — options are either generically defaulted or required
   only when the consuming feature is enabled.
5. **Minimum-viable principle**: a fleet declaring one host and one provider
   must evaluate. Never add an unconditionally-required setting unless the
   base layer consumes it on every host (currently only `adminSshKeys` and
   `sysadmin_ssh_key`).
6. **Secrets are SOPS-only.** The store is scaffolded by `fleet secrets init`
   and wired via `mkFleet { secretsFile = …; }`. Never commit plaintext
   secrets, never bake credentials into images or modules; modules declare
   needs via `sopsLib.mkSecret`.

## Discovering the API

- **Start here (agents):** `nix build .#introspection` → one JSON manifest
  with the whole "how do I drive fleetkit" surface — the CLI command tree
  (each verb's help + params), the option surface, and the component
  interfaces. Or run it live and env-free: `nix run .#fleet -- describe`
  (identical data; reads no creds/SOPS). The `introspection-surface` gate
  keeps it whole; the CLI walk cannot drift from the actual commands.
- `nix build .#docs` → options reference site (chapters per option group:
  `fleet/*`, `infra/<stratum>`), generated from the module declarations —
  it cannot drift from the code.
- `nix build .#options-json` → the same option data as structured JSON
  (name, type, default, description, declaring file) — also folded into
  `.#introspection`.
- `fleet --help` (and per-group `--help`) → the operational surface.
- `templates/minimal/` → a complete working consumer, kept minimal on
  purpose; the commented sections enumerate the full settings surface.
- `nix/checks.nix` header → what each acceptance gate proves.

## Working in a consumer repo

- The consumer's flake calls `fleetkit.lib.mkFleet { modules; backend;
  secretsFile; … }`; its manifest modules declare `fleet.compute.*`,
  `fleet.providers.*`, `fleet.settings.*`.
- Iterate on the framework locally with
  `--override-input fleetkit path:/path/to/fleetkit`.
- Consumer repos typically carry their own agent instructions (CLAUDE.md /
  AGENTS.md) with fleet-specific context; this file governs the framework
  itself.
- Observability MCP endpoints (Grafana, Tempo, …) are consumer concerns —
  their URLs come from that fleet's `fleet.settings.observability.*`. The
  `infra.integrations.mcp` module lets a fleet declare in-fleet MCP servers per host.

## Contribution shape

Small, verified steps: change → `nix flake check` → conventional commit.
Framework code must work for a fleet that is not this repo's origin — when in
doubt, ask "would this hold for a two-host homelab on one Proxmox node?"
