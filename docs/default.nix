# Options documentation site (mdBook), NixOS-WSL style:
#   nixosOptionsDoc renders every option fleetkit declares to CommonMark,
#   which lands in the mdBook alongside the hand-written pages.
#   `nix build .#docs` → static site in result/.
#
# One NixOS eval covers both option trees: fleet.* (manifest schema) and
# infra.* (service modules). Options declared OUTSIDE this repo (NixOS
# itself, sops-nix, disko) are hidden via transformOptions.visible, and
# declaration links are rewritten to GitHub.

{ pkgs, nixpkgs, sops-nix, disko, lib ? pkgs.lib }:

let
  # One shared NixOS eval of fleetkit's option trees (fleet.* + infra.*),
  # factored into nix/components/eval.nix so the component module-schema
  # checks lock exactly the option paths these docs render.
  shared = import ../nix/components/eval.nix { inherit pkgs nixpkgs sops-nix disko lib; };
  inherit (shared) fleetkitRoot eval isOurs;

  githubBase = "https://github.com/alexanderjerome/fleetkit/blob/main";

  optionsDoc = pkgs.nixosOptionsDoc {
    options = eval.options;
    warningsAreErrors = true;
    transformOptions = opt:
      opt
      // { visible = (opt.visible or true) && isOurs opt; }
      // {
        declarations = map (d:
          let rel = lib.removePrefix (fleetkitRoot + "/") (toString d);
          in if lib.hasPrefix fleetkitRoot (toString d)
             then { name = rel; url = "${githubBase}/${rel}"; }
             else d)
          opt.declarations;
      };
  };

in pkgs.stdenv.mkDerivation {
  name = "fleetkit-docs";
  passthru.optionsJSON = optionsDoc.optionsJSON;
  src = ./.;
  nativeBuildInputs = [ pkgs.mdbook pkgs.python3 ];
  buildPhase = ''
    # Structured chapter tree from the options JSON (one page per
    # option group + generated SUMMARY) — not the flat CommonMark dump.
    python3 generate.py ${optionsDoc.optionsJSON}/share/doc/nixos/options.json src
    mdbook build
  '';
  installPhase = ''
    mv book $out
  '';
}
