# Schema notes

## Loose blocks (allowed, inventoried)

| path                                   | why loose                                         | ref leaves that must still be validated |
| -------------------------------------- | ------------------------------------------------- | --------------------------------------- |
| `fleet.estates.<e>.git`                | large provider-shaped settings (organization, actions, rulesets, auth union) | infrastructureRepo, backend, members.*, teams.*.members, teams.*.repos[].repo, rulesets.*.repositories, actions.secrets.*.repos, auth.*Ref / actions.secrets.*.sourceRef (declared sops key of the same estate) |
| `fleet.estates.<e>.cache`              | substituter union (host+network vs url)           | substituters[].host (guest or node), substituters[].network |
| `fleet.estates.<e>.build`              | build farm settings                               | hydra.guest, workers[].host (guest or node), workers[].network |
| `fleet.estates.<e>.observability`      | push targets                                      | *.host (guest), *.network |
| `fleet.estates.<e>.substrate`          | role -> guest lists                               | network, every role list (guest), secretsEngine.guests (guest) |
| `fleet.estates.<e>.gpu`                | device list                                       | devices (pcie) |
| `fleet.estates.<e>.llm`                | gateway settings                                  | gateway (guest), reach (network) |
| `fleet.estates.<e>.storage`            | pool list                                         | pools (storage) |
| `fleet.estates.<e>.colmena`            | deployment knobs                                  | none |

The only untyped type in `modules/` is the `looseBlock` helper in
`modules/loose.nix`; there is no other `types.anything`, `unspecified`,
`raw` or `attrs`. `tests/loose_blocks.sh` enforces this: it fails if any other
untyped type appears, if the number of `looseBlock` uses differs from
`fleet.report.looseBlocks`, or if the table above differs (with `<e>` read as
`<estate>`) from `nix eval .#fleet.report.looseBlocks`. A block that cannot be
typed is added to the table, to `fleet.report.looseBlocks` and to a
`looseBlock` use together.

### Derived, read-only, typed (not loose blocks)

- `fleet.report.providerView.<estate>.<guest>` = { resource; args; lifecycle;
  companions }: the bpg/proxmox 0.115.0 arguments each guest renders to. Its
  type is a recursive JSON value type built from typed primitives (str, int,
  bool, float, null, listOf/attrsOf of itself); no `types.anything`, `raw` or
  `attrs`, so `tests/loose_blocks.sh` still holds. Never set by config.
- `fleet.report.guestOptionPaths`: the 93 guest option paths, each mapped in
  `docs/guest-provider-map.md` and checked by `tests/guest_fidelity.py`
  (gate `fidelity`).
- `fleet.report.anchorDrift.<estate>`: derived recipient anchors vs guests
  (data, never an evaluation failure); `tests/anchors.py` runs under the
  `parity` gate.
- `fleet.estates.<e>.guestDefaults`: the typed guest knob set (declared in
  `modules/guests.nix` by extending the estates submodule), the layer between
  `sites.<s>.providers.proxmox.defaults` and each guest. Not loose.
  `fleet.report.looseBlocks` is unchanged by the guest model.
