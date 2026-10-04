# Add-ons: optional, opt-in integrations with niche external Nix tooling.
#
# An add-on is a directory here holding three files:
#   options.nix   the `infra.addons.<name>` option declarations (always
#                 imported, so the options are documented and schema-checked
#                 and a typo fails with "option does not exist" rather than
#                 silently doing nothing)
#   default.nix   the NixOS module (`config`), inert unless the add-on is
#                 enabled on that host
#   checks.nix    its acceptance gate(s), merged into `nix flake check` by
#                 ./checks.nix
#
# An add-on's SOURCE is never vendored here: the consuming repo owns the
# flake input (and so its version and lock) and hands it to
#
#   fleetkit.lib.mkFleet { addons.<name> = inputs.<name>; ... }
#
# so a fleet that does not use an add-on neither locks nor fetches it, and
# the minimum-viable fleet (one host, one provider) evaluates unchanged.
{ lib, ... }:
{
  imports = [
    ./llm-agents/options.nix
    ./llm-agents/default.nix
  ];

  options.infra.addons.inputs = lib.mkOption {
    type = lib.types.attrsOf lib.types.raw;
    default = { };
    internal = true;
    description = ''
      Flake inputs the consuming repo handed to mkFleet's `addons` argument,
      keyed by add-on name. Set by mkFleet; not meant to be set by hand.
    '';
  };
}
