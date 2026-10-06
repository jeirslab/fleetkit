# sopsRef: resolve a secret reference sops:<estate>/<file>#<key> for the
# renderers (terraform.nix, github.nix). Evaluation only.
#
#   import ./sops-ref.nix { fleet; estate; where; } ref
#     -> { name; path; key; expr; }
#
# The ref is resolved through the secrets of the estate it NAMES, whichever
# estate is rendered. `name` is the data.sops_file alias: the file alias, or
# <owner>_<alias> when the file belongs to another estate, so the two cannot
# collide. `path` is the file's path, `key` the key as carlpett/sops flattens
# a nested document (nested keys joined with "."; sops/flatten.go v1.4.1),
# and `expr` the Terraform reference to the value.
{
  fleet,
  estate,
  where,
}:
ref:
let
  m = builtins.match "sops:([^/#]+)/([^#]+)#(.+)" ref;
  owner = builtins.elemAt m 0;
  file = builtins.elemAt m 1;
  files = fleet.estates.${owner}.secrets.files or { };
  name = if owner == estate then file else "${owner}_${file}";
  key = builtins.replaceStrings [ "/" ] [ "." ] (builtins.elemAt m 2);
in
if m == null then
  throw "${where}: reference \"${ref}\" is not sops:<estate>/<file>#<key>"
else if !(fleet.estates ? ${owner}) then
  throw "${where}: reference \"${ref}\" names estate \"${owner}\", which is not declared"
else if !(files ? ${file}) then
  throw "${where}: reference \"${ref}\": fleet.estates.${owner}.secrets.files has no \"${file}\""
else
  {
    inherit name key;
    inherit (files.${file}) path;
    expr = "\${data.sops_file.${name}.data[\"${key}\"]}";
  }
