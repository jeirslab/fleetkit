{ config, lib, pkgs, ... }:

# Platform substrate dispatch.
#
# `infra.platform.type` is the "what am I running on" knob. Every per-
# substrate config block (boot loader, kernel modules, console params,
# fileSystems) lives under ./pve/ or ./xcpng/ and gates itself on this
# value via mkIf.
#
# Enum values are fully qualified (substrate.variant) so the scope is
# unambiguous when grep'd: "pve.lxc" is clearly Proxmox-LXC, never an
# orphan "lxc" string that might mean anything.
#
# Wiring: nix/lib/default.nix sets infra.platform.type from each fleet
# entry's substrate-aware pve_type field (nix/fleet/default.nix derives
# it from kind + provider_instance). Manual override per-host only when
# auto-detection is wrong.

{
  imports = [
    ./pve
    ./xcpng
    ./baremetal.nix
  ];

  options.infra.platform.type = lib.mkOption {
    type = lib.types.enum [ "pve.lxc" "pve.qemu" "xcpng.vm" "baremetal" ];
    default = "pve.lxc";
    description = ''
      Virtualisation substrate this host runs on:
        - pve.lxc   : Proxmox-hosted unprivileged LXC container
        - pve.qemu  : Proxmox-hosted KVM/QEMU VM
        - xcpng.vm  : XCP-ng-hosted Xen HVM VM (tier-0 + select tier-1)
        - baremetal : physical or externally-provisioned host that carries its
                      own hardware-configuration.nix, bootloader, and network
                      stack (no hypervisor-guest assumptions)

      Auto-wired by nix/lib/default.nix from the fleet entry; override
      per-host only when the auto-detection is wrong.
    '';
  };

  # ── Auto-grow disks on VM platforms ────────────────────────────
  # Fleet-wide policy: any disk declared on a VM (PVE QEMU or XCP-ng)
  # should be non-destructively grow-able. After `fleet xoa resize-disk`
  # or PVE-side disk grow, the next boot expands both the partition
  # (if there is one) and the filesystem to fill the new capacity.
  #
  # PVE LXCs are excluded: containers share the host kernel and don't
  # manage block devices at this layer; their rootfs grows
  # automatically when PVE resizes the underlying volume.
  config = lib.mkIf (lib.elem config.infra.platform.type [ "pve.qemu" "xcpng.vm" ]) {
    # Root partition: cloud-utils `growpart` runs in stage-1 boot,
    # then `resize2fs` grows the root filesystem. Handles the case
    # where the bootstrap template was built at 32 GB but the
    # cloned VM has a 64+ GB root disk.
    boot.growPartition = true;

    # Additional data disks (xvdb / xvdc / etc., mounted via
    # fileSystems): walk every ext4/xfs filesystem at boot and run
    # the appropriate growfs tool. Idempotent — `resize2fs` /
    # `xfs_growfs` is a no-op when the filesystem already fills
    # its underlying block device. Covers both partitioned and
    # raw-block-device data disks.
    systemd.services.fs-autogrow = {
      description = "Auto-grow ext4/xfs filesystems to fill underlying disks";
      wantedBy = [ "local-fs.target" ];
      after = [ "local-fs.target" ];
      before = [ "multi-user.target" ];
      path = with pkgs; [ util-linux e2fsprogs xfsprogs ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
      };
      script = ''
        findmnt -nr -t ext4,xfs -o SOURCE,TARGET,FSTYPE | \
          while read src tgt fstype; do
            case "$fstype" in
              ext4) resize2fs "$src" 2>&1 || true ;;
              xfs)  xfs_growfs "$tgt" 2>&1 || true ;;
            esac
          done
      '';
    };
  };
}
