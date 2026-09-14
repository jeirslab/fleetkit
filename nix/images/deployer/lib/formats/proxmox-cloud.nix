# Custom nixos-generators format: Proxmox cloud-init VM disk.
#
# nixos-generators' bundled `proxmox` format selects `system.build.VMA` — the
# vzdump `.vma.zst` archive restored with `qmrestore`. This sibling selects
# `system.build.cloudImage` from the SAME module (proxmox-image.nix): a raw
# disk (`make-disk-image` with `format = "raw"`, no `postVM`), meant to be
# registered as a template and cloned with a cloud-init drive. Cloud-init is
# already on by default (`proxmox.cloudInit.enable`), so the difference from
# the VMA target is purely packaging — a plain disk instead of a backup archive.
#
# In nixpkgs, `proxmoxImage == proxmoxVMA` (an alias) and `proxmoxCloudImage`
# is `cloudImage`; so the three upstream jobs are two distinct artifacts, and
# this format is the second one.
{ modulesPath, ... }:
{
  imports = [ "${toString modulesPath}/virtualisation/proxmox-image.nix" ];

  formatAttr = "cloudImage";
  fileExtension = ".img";
}
