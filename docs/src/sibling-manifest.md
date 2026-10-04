# Sibling estates and the estate manifest (proposal)

Status: **proposal**, for review. Nothing here is implemented.

## Problem

Two estates that run on the same hardware cannot see each other. The homelab and a company
estate share Proxmox nodes, one IP/VMID space and one GPU server, so every decision on one side
(a new guest, an address, a GPU window) can collide with the other, and an agent or developer
working in one repo has no context for the other. Embedding the other repo (submodule or flake
input) answers "what is free?" with the other side's whole codebase and release cadence.
What is wanted is the **facts**, not the source.

## Terms

- **Estate**: a consumer's whole fleet plus the substrate it sits on (`fleet.settings`,
  `fleet.network`, sites). A **sibling estate** is another estate, in another repository, that
  this one has chosen to exchange manifests with.
- **Provider / tenant** is the usual shape: one estate offers hardware and shared services, the
  other declares workloads that run on it. The relation is declared, not implied.
- **Manifest**: a JSON document an estate produces from its own evaluated config.

## Goals

1. `fleet manifest` produces a **complete** manifest of the local estate (owner's eyes only).
2. **Selective export**: a filtered manifest per sibling, safe to commit into that sibling's repo.
3. `fleet manifest --schema`: the **type schema only** (paths, types, descriptions, visibility;
   no values), in the same shape as `nix/components/schema/*.json` and the docs generator.
4. A consumer declares `infra.siblings.<name>` and reads the vendored manifest as typed values.
5. Fully automatic: generated from the evaluated config, refreshed by CI as a PR.

## Non-goals

- Controlling a sibling. At most: opening a PR or triggering a workflow in the sibling's own
  repository. Each estate stays sovereign over its own state.
- Sharing OpenTofu state. Each estate keeps its own backend.
- Putting secret **values** in the manifest (they travel only in the generated sops bundle).

## What the manifest is built from

Not a walk of the raw NixOS `config` (huge, full of secret placeholders). It is built from
fleetkit's typed surfaces: `fleet.compute`, `fleet.network`, `fleet.settings`, sites, the component
registry/schema, and add-on outputs (for example the `llm-agents` catalog). Anything an option
exposes is already typed and described, so visibility can be a property of the option.

Candidate v0 contents:

| Section | Facts |
|---|---|
| nodes | Proxmox nodes available to the sibling, free cores/RAM/disk, datastores and pools it may use |
| network | CIDR(s), gateway, DNS, **VMID range**, **IP range** (free and claimed) |
| gpu | cards, current holder, exclusivity group, schedule (see below) |
| endpoints | shared service URLs and model catalogs (the "shared variables") |
| secrets | everything except the value: name, file, host, owner/mode, restart units, recipients |

## Visibility (deny by default)

Every exported item carries a visibility: `private` (default), `estate`, or `sibling:<name>`.
Unmarked means private, so a new option cannot leak by omission. The exporter also refuses any
value that originates from a sops placeholder or a secret-typed option, whatever its mark.
Key names follow the same filter: the type schema (`hosts.<name>.ip : string`) is always safe;
the shape of an instance (which host names exist) is not.

## Secrets

The manifest describes every secret in full **except its value**: name, key inside the file, source
file, owning host, owner/mode, restart units and the recipient list. Anyone reading it, human or
agent, can see what exists and who can decrypt it, never what it says.

What a sibling must actually use is delivered as **one generated sops file** per sibling, holding
only the values that sibling needs, encrypted to specific recipients (its developer machines and
hosts). The sibling keeps owning its own secrets; the bundle only adds what the provider shares.

The generator must decrypt the source secrets and re-encrypt them into the bundle, so it runs where
an admin key exists (the owner's machine, or a CI job with a dedicated key), not in an arbitrary
pipeline. Revoking a recipient does not undo what it already read: rotate. git-crypt is deliberately
not used (a second key system, and an encrypted manifest blinds agents).

## Power schedules and exclusion groups

Shared hardware that cannot be used by two guests at once (a passthrough GPU) needs a declared
coordination primitive:

- a per-guest **schedule** (up/down windows, explicit time zone);
- an **exclusion group**: at most one member up;
- an executor using the Proxmox API with a token limited to power actions on the named VMIDs;
  order is always stop, confirm stopped, then start; a **hold** flag pauses the schedule;
- an optional **is-it-safe-to-stop** hook, so a job-aware scheduler can sit on top and refuse to
  stop a guest that has undrained work.

## Network facts

Each estate exports its VMID range, CIDR and IP range. The consumer reads them instead of
guessing, and a check fails the build when two estates claim the same address or VMID. A dedicated
subnet per estate (for example behind a VyOS router) removes the collision class; the manifest is
what makes moving to it safe.

## Delivery

`fleet siblings refresh` (or a scheduled CI job) fetches the sibling's latest manifest with the
operator's GitHub permissions and commits it as `siblings/<name>.json`. A vendored snapshot means
no network or auth at evaluation time, and git history shows exactly what changed.

## Phasing

1. Manifest schema, `fleet manifest` (+ `--schema`), network facts, visibility marks.
2. `infra.siblings` consumer side, collision check, refresh command, secrets convention.
3. Power schedules and exclusion groups.

## Open questions

- Confirm how `estate`, `fleets` (ADR-097 namespaces) and `sites` relate, and where a sibling
  estate attaches, before fixing the option names.
- Where the visibility mark lives for add-on and component outputs.
- Whether the consumer side validates a sibling manifest against its published schema, and how
  schema versions are negotiated.
- Overlap with job-aware compute scheduling that a consumer may build for itself.
