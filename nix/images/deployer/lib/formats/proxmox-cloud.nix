# Custom nixos-generators format: Proxmox cloud-init VM disk.
#
# nixos-generators' bundled `proxmox` format selects `system.build.VMA` — the
# vzdump `.vma.zst` archive restored with `qmrestore`. This sibling builds the
# SAME system (proxmox-image.nix: cloud-init on, GRUB on virtio0, growpart) as
# a plain disk image instead of a backup archive — the shape PVE's
# `qm importdisk` / the bpg provider's `disk.file_id` consume: upload to a
# storage with `iso` content, import as the VM's root disk, finish through the
# cloud-init drive. No hypervisor shell, no `qmrestore`, no template VMID.
#
# Why this format defines `system.build.cloudImage` itself: nixpkgs'
# proxmox-image.nix exports only `system.build.VMA` (and `image`, an alias of
# it — `proxmoxImage == proxmoxVMA` in release.nix); there is no upstream
# cloud-image artifact to select, so an earlier revision of this file that
# pointed `formatAttr` at a non-existent `cloudImage` failed at eval. The
# disk is built with the module's own partition settings and no `postVM`.
#
# qcow2-compressed rather than raw: the file is uploaded over the consumer's
# uplink on every apply that rebuilds it, and compressed qcow2 is a fraction
# of the raw size while `qm importdisk` probes the format itself. PVE's `iso`
# content type refuses a `.qcow2` extension, so consumers name the UPLOADED
# file `.img`; the content is still qcow2 (the same convention stock Debian
# genericcloud images are uploaded under).
{ config, lib, pkgs, modulesPath, ... }:
{
  imports = [ "${toString modulesPath}/virtualisation/proxmox-image.nix" ];

  formatAttr = "cloudImage";
  fileExtension = ".qcow2";

  system.build.cloudImage = import "${toString modulesPath}/../lib/make-disk-image.nix" {
    name = "proxmox-cloud-${config.proxmox.filenameSuffix}";
    # → $out/nixos-bootstrap-cloud.qcow2 (make-disk-image appends the
    # format's extension). Stable on purpose: the tf file emitter globs
    # `*.qcow2`, the deployer CLI reads `targets.proxmox-vm-cloud.artifact`.
    baseName = "nixos-bootstrap-cloud";
    inherit (config.proxmox) partitionTableType;
    inherit (config.proxmox.qemuConf) additionalSpace bootSize;
    inherit (config.virtualisation) diskSize;
    format = "qcow2-compressed";
    inherit config lib pkgs;
  };
}
