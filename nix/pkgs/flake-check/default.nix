# flake-check — update a flake's inputs (all, or the ones named), prove
# every nixosConfiguration and check still evaluates, and report as JSON
# (markdown rendered from it); on a regression, name the input that caused
# it (see the script header). Run by
# .github/workflows/flake-check.yml inside a consumer repo, or by hand:
# `nix run github:jeirslab/fleetkit#flake-check`. `nix` itself comes from the
# caller's PATH, so its version and config (substituters, access-tokens) are
# the host's.
{ writeShellApplication, jq, coreutils, gnugrep, gnused }:

writeShellApplication {
  name = "flake-check";
  runtimeInputs = [ jq coreutils gnugrep gnused ];
  text = builtins.readFile ./flake-check.sh;
}
