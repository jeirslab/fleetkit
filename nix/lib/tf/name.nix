{ lib }:

# Terraform resource NAME for a fleet key.
#
# A fleet key is any attr name (`netcore-2`, `"1010"`); a Terraform
# resource name must start with a letter or underscore and may contain
# only letters, digits, underscores and dashes. A CTID-keyed estate
# (ADR-100 §2.1 on the incumbent consumer: `lxc."1010"`) would otherwise
# render `"1010": {…}` and tofu refuses the whole leaf ("Invalid resource
# name"). So: a key that is already a valid identifier is used as is —
# every existing address is unchanged — and any other key is prefixed
# with `_` after replacing invalid characters with `-`:
#
#   "1010"      → "_1010"     proxmox_virtual_environment_container._1010
#   "netcore-2" → "netcore-2" (unchanged)
#
# Every emitter that derives a resource name, a depends_on or an
# interpolation from a compute key MUST go through this, or the address
# and the reference disagree. The fleet key itself (colmena node,
# `fleet deploy nixos apply host 1010`, hostsJson) never changes.
let
  valid = k: builtins.match "[A-Za-z_][A-Za-z0-9_-]*" k != null;
  clean = k: lib.concatStrings (map (c: if builtins.match "[A-Za-z0-9_-]" c != null then c else "-")
                                    (lib.stringToCharacters k));
in {
  tfName = key: if valid key then key else "_${clean key}";
}
