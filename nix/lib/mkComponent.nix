# nix/lib/mkComponent.nix — the component descriptor.
#
# A component is a freely-refactorable leaf (a NixOS module, a terranix
# emitter, or an image builder) whose EXPORTED INTERFACE is locked by a
# committed schema (see ./../components/checks.nix), while its internals stay
# free. `mkComponent` is NOT a wrapper in the eval path: it returns metadata
# plus the leaf passed through UNCHANGED (`imports = [ src ]`), so the hot
# eval path (mkFleet / host / tf) is byte-for-byte what it was before. Its
# only eval-time effect is asserting the leaf's required subdirs exist, which
# fails a bad refactor early with a clear message.
{ lib }:

{
  # "module" | "tf" | "image"
  family,
  # component identity, e.g. "infra.network.dns", "tf.compute.proxmox",
  # "images.proxmox-lxc". Also the schema file's basename.
  name,
  # the leaf source: a path (module/tf) imported as-is, or an image builder.
  src,
  # subdirs that MUST exist under `src` (eval-time assertion), e.g.
  # [ "options" ] for a module leaf so the schema has a stable boundary.
  requires ? [ ],
  # family-specific interface descriptor consumed by the schema check
  # (e.g. an image's templateRef; modules derive theirs from the eval).
  interface ? { },
}:

assert lib.assertMsg (builtins.elem family [ "module" "tf" "image" ])
  "mkComponent(${name}): unknown family '${family}' (module|tf|image)";
assert lib.all (
  sub:
  lib.assertMsg (builtins.pathExists (src + "/${sub}"))
    "mkComponent(${name}): required subdir '${sub}' missing under ${toString src} (did you `git add` it?)"
) requires;

{
  inherit family name src interface;
  # Identity pass-through: what an aggregator splices into `imports = [ … ]`
  # is exactly the leaf it always was.
  imports = [ src ];
}
