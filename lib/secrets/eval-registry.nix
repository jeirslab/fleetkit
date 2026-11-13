# ADR-0009, ADR-0017: lib.secrets.evalRegistry. Evaluate a registry on its own:
# the schema (modules/secrets.nix), the standalone stand-in for the fleet model
# (lib/secrets/standalone.nix) and the
# registry module. Needs nixpkgs' lib only, so it also runs under
# `nix-instantiate --eval --store dummy://` (tests/e2e.sh).
#
#   import ./eval-registry.nix { inherit lib; } { registry; extraModules ? [ ]; }
#     -> { config; options; failures; ok; guard; checkReport; }
{ lib }:
{
  registry,
  extraModules ? [ ],
}:
let
  ev = lib.evalModules {
    modules = [
      ../../modules/secrets.nix
      ./standalone.nix
      registry
    ]
    ++ extraModules;
  };
  failures = map (a: a.message) (lib.filter (a: !a.assertion) ev.config.assertions);
  # Throws with every failed assertion, else returns v.
  guard =
    v:
    if failures == [ ] then
      v
    else
      throw "secrets: the registry has errors:\n  - ${lib.concatStringsSep "\n  - " failures}";
in
{
  inherit (ev) config options;
  inherit failures guard;
  ok = failures == [ ];
  # report.check of a data repository (what tools/secrets-check.py reads).
  checkReport =
    allow:
    guard {
      inherit (ev.config.fleet.report.secrets) sopsConfig files;
      allow = {
        missingKeys = allow.missingKeys or [ ];
        recipients = allow.recipients or [ ];
      };
    };
}
