# Acceptance gates contributed by add-ons, merged into the flake's `checks`
# next to nix/checks.nix. Each add-on keeps its own checks.nix; list it here
# (fleetkit hand-lists modules and gates, there is no directory scan).
{ nixpkgs, mkFleet }:
(import ./llm-agents/checks.nix { inherit nixpkgs mkFleet; })
