# flake-check — update a flake's inputs (all, or the ones named) and prove
# every nixosConfiguration and check still evaluates; on a regression, name
# the input that caused it (see the script header). Run by
# .github/workflows/flake-check.yml inside a consumer repo, or by hand:
# `nix run github:jeirslab/fleetkit#flake-check`. `nix` itself comes from the
# caller's PATH, so its version and config (substituters, access-tokens) are
# the host's.
{ writeShellApplication, jq, coreutils, diffutils, gawk, gnugrep, gnused }:

writeShellApplication {
  name = "flake-check";
  runtimeInputs = [ jq coreutils diffutils gawk gnugrep gnused ];
  text = builtins.readFile ./flake-check.sh;
}
