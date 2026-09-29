{ config, lib }:

# XCP-ng / XOA emitters. The Vates provider (`vatesfr/xenorchestra` —
# historical naming) talks to the XOA control plane; identifiers here
# stay `xenorchestra_*` to match the provider's resource types.
#
# Scope rule: the only resource.* blocks emitted are
# xenorchestra_vm and xenorchestra_cloud_config. Pools, networks, SRs,
# and templates are read-only via data.xenorchestra_*. They must
# already exist on the XCP-ng host (use xo-cli list-objects to
# discover their name_labels).

let
  inherit (import ./name.nix { inherit lib; }) tfName;
  # Per-VM cloud-init renderer — shared with the Proxmox emitter via
  # the extracted helper at nix/lib/cloud-init.nix.
  inherit (import ../cloud-init.nix { inherit config lib; })
    renderCloudInitSnippet;

  # Destruction-policy-aware lifecycle block. Mirrors mkLifecycle in
  # _lib/proxmox.nix:132-150 — `xen-orchestra.main` is configured with
  # destruction_policy = "strict", which means every resource emitted
  # under this provider always gets prevent_destroy = true.
  #
  # XCP-ng/XOA-specific: disk size changes on xenorchestra_vm are
  # ForceNew in the vatesfr/xenorchestra provider. To grow a disk
  # without TF destroying it (and the data on it), we ignore size
  # drift. Growth is operator-driven via `fleet xoa resize-disk` →
  # `xo-cli vdi.set` + `resize2fs` inside the guest. The fleet
  # `size_gb` is the *intent at first apply*; live size is owned by
  # the operator afterwards.
  mkXoLifecycle = meta:
    let
      parts = lib.strings.splitString "." meta.provider_instance;
      inst = builtins.elemAt parts 1;
      instCfg = config.fleet.providers.xen-orchestra.${inst} or null;
      policy = if instCfg == null then "standard" else instCfg.destruction_policy;
      # "transient" tag opts out of the provider's strict
      # destruction_policy — by definition these workloads are
      # build-on-demand (xo-installer-v10 et al, see
      # fleet.compute.<name>.enabled). They need to round-trip
      # destroy → create through Terraform, which prevent_destroy
      # blocks. Stateful-tag check still applies regardless: a
      # "bitcoin transient" entry would still be protected by the
      # bitcoin tag (and would fail validation up-front for setting
      # both tags, so the combo isn't expressible).
      isTransient = meta ? tags && lib.elem "transient" meta.tags;
      effectiveProtect = meta.protect
        || (policy == "strict" && !isTransient)
        || (meta ? tags
            && lib.intersectLists meta.tags config.fleet.STATEFUL_TAGS != []
            && policy != "permissive");
      # Each disk's .size is ignored — see comment above. Tofu HCL only
      # accepts static index paths (no `[*]` wildcard in ignore_changes
      # JSON), so we enumerate one entry per declared disk. Other disk
      # attributes (sr_id, name_label) stay tracked, and adding/removing
      # disks still propagates because the list length is in fleet.
      xoaDisks = meta.xoa.disks or [];
      diskSizeIgnores = lib.imap0 (i: _: "disk[${toString i}].size") xoaDisks;

      # `template` is ignored fleet-wide. XOA assigns a fresh UUID
      # whenever a template is re-imported (e.g. via `xo-cli vm.import`
      # + `vm.convertToTemplate` for a refreshed nixos-fleet-bootstrap
      # build) even when the `name_label` is identical. Without this
      # ignore, `data.xenorchestra_template.<ref>.id` returns the new
      # UUID after re-import → every xenorchestra_vm referencing it
      # sees `~ template = "<old>" -> "<new>" # forces replacement` and
      # tofu proposes to destroy + recreate every VM in the stack on
      # the next apply. With the ignore, the template attr is set at
      # create time and never drift-compared afterwards. To intentionally
      # rebuild a VM on a newer template, use `tofu apply -replace=...`.
      templateIgnore = [ "template" ];

      ignoreChanges = diskSizeIgnores ++ templateIgnore ++ (meta.ignore_changes or []);
    in
      {
        lifecycle = [(lib.mkMerge [
          (lib.optionalAttrs effectiveProtect { prevent_destroy = true; })
          (lib.optionalAttrs (ignoreChanges != []) { ignore_changes = ignoreChanges; })
        ])];
      };

  providerAlias = meta:
    "xenorchestra.${builtins.elemAt (lib.strings.splitString "." meta.provider_instance) 1}";
in rec {
  # ── xenorchestra_vm emitter ────────────────────────────────────
  # Clones a template into a VM, attaches the cloud-init resource,
  # wires up NICs from fleet.compute.<name>.xoa.networks, and the
  # additional VDIs from fleet.compute.<name>.xoa.disks. Memory is
  # accepted in MiB on fleet.compute but the provider takes bytes.
  # True iff this VM wants a cloud-init resource attached. NixOS
  # VMs typically don't — the bootstrap template already ships with
  # the sysadmin key for root, and the host's NixOS config (via
  # Colmena) handles users + hostname. Only emit cloud-config when
  # the fleet entry explicitly declares cloud_init.users.
  wantsCloudConfig = meta:
    ((meta.cloud_init.users or []) != [])
    || ((meta.cloud_init.vyos_config_commands or []) != []);

  mkXoVm = name: meta:
    let
      xoa = meta.xoa;
      mkNic = nic:
        { network_id = "\${data.xenorchestra_network.${nic.ref}.id}"; }
        // lib.optionalAttrs (nic.mac != null) { mac_address = nic.mac; };
      mkDisk = d: {
        sr_id      = "\${data.xenorchestra_sr.${d.sr_ref}.id}";
        name_label = d.name;
        size       = d.size_gb * 1024 * 1024 * 1024;
      };
      base = {
        provider     = providerAlias meta;
        name_label   = name;
        cpus         = meta.cpu_cores;
        memory_max   = meta.memory_mb * 1024 * 1024;
        tags         = meta.tags;
        template     = "\${data.xenorchestra_template.${xoa.template}.id}";
        network      = map mkNic xoa.networks;
        # xenorchestra_vm requires >= 1 disk block — the template's root
        # disk is described here too, not just additional disks.
        disk         = map mkDisk xoa.disks;
        # Force UEFI HVM firmware. The "Other install media" base template
        # defaults to "bios", but BIOS+GRUB on Xen HVM hit a kernel
        # early-init hang (v3/v4 with disko bios_boot+GRUB both wedged
        # right after the decompressor's "Booting the kernel" line; even
        # nokaslr + earlyprintk didn't help). UEFI + systemd-boot is the
        # well-trodden NixOS path and worked cleanly in v2; we just had
        # the firmware mismatch (image UEFI, VM BIOS). Pinning to UEFI
        # here matches the disko ESP+systemd-boot layout in
        # nix/images/by-platform/xen-orchestra.nix.
        hvm_boot_firmware = "uefi";
      }
      // (lib.optionalAttrs (wantsCloudConfig meta) {
        # .template (the rendered #cloud-config CONTENT), not .id — the
        # provider passes this string verbatim into the drive's user-data
        # file. Wiring .id shipped a bare UUID as user-data, which also
        # made cloud-init skip the sibling network-config (INFRA-194;
        # first real exercise of this path — NixOS VMs don't use it and
        # VyOS went the prebaked-template route instead).
        cloud_config = "\${xenorchestra_cloud_config.${tfName name}.template}";
      })
      // (lib.optionalAttrs ((xoa.cloud_network_config or null) != null) {
        # Cloud-init network-config v2, verbatim from fleet. Rendered into
        # the NoCloud drive alongside user-data. Match by MAC (pinned in
        # xoa.networks) — Xen HVM guests name NICs enX0/enX1, not eth0.
        cloud_network_config = xoa.cloud_network_config;
      })
      // (lib.optionalAttrs ((xoa.iso_ref or null) != null) {
        # Boot from ISO on first power-on. References a data.xenorchestra_vdi
        # lookup declared in fleet.resources (kind = "xo-iso"). Useful for
        # one-shot OS installs onto blank disks — once installed, eject
        # the CD via `xo-cli vm.ejectCd id=<vm>` and the VM boots from
        # disk on next start. The provider currently doesn't expose a
        # boot_order attr on xenorchestra_vm; if the underlying VM's
        # boot order is `dc` (CD-then-disk, XOA's default for UEFI) the
        # ISO takes precedence on first boot.
        cdrom = [{
          id = "\${data.xenorchestra_vdi.${xoa.iso_ref}.id}";
        }];
      });
    in
      base // (mkXoLifecycle meta);

  # ── xenorchestra_cloud_config emitter ──────────────────────────
  # Per-VM userdata, generated from the shared cloud-init renderer.
  # No lifecycle block on this one — cloud-init updates must flow
  # through normally; the VM's prevent_destroy still gates teardowns.
  mkXoCloudConfig = name: meta: {
    provider = providerAlias meta;
    name     = "${name}-cloud-init";
    template = renderCloudInitSnippet name meta;
  };

  # ── Boot-order protocol (INFRA-39) ─────────────────────────────
  # Exactly TWO boot profiles exist fleet-wide — no third:
  #
  #   persistent (default)        → order=cnd  (Disk → Net → CD)
  #     Every deployed host of any OS (PVE, PBS, NixOS). All three
  #     methods stay active: a blank disk falls through to iPXE
  #     when one is serving, then to the install CD; once installed
  #     the disk matches first. Installs become one-shot — no CD
  #     eject, no post-install boot-order surgery.
  #
  #   transient ("transient" tag) → order=dnc  (CD → Net → Disk)
  #     Liveboot/throwaway VMs whose workload IS the CD (installer
  #     shells, rescue images). CD always wins; disk stays last as
  #     a recovery path.
  #
  # The vatesfr/xenorchestra provider doesn't expose boot_order on
  # xenorchestra_vm, so the protocol is enforced by a sibling
  # terraform_data running xo-cli vm.setBootOrder post-create. The
  # desired order is part of triggers_replace: changing a VM's
  # profile (or the protocol itself) replaces the terraform_data
  # and re-runs the provisioner on the next apply — that's the
  # reconciliation path for VMs that drifted or predate INFRA-39.
  #
  # local-exec dependency: nodejs (via `nix shell nixpkgs#nodejs`) + npx
  # to fetch xo-cli on the fly. xo-cli reads its config from
  # ~/.config/xo-cli/config.json on the host where `fleet deploy tf
  # apply` runs.
  #
  # Failures fail the apply on purpose (no on_failure=continue): a
  # silently-skipped setBootOrder is exactly how the fleet drifted
  # into five different boot orders pre-INFRA-39. The tainted
  # terraform_data simply retries on the next apply.
  bootOrderFor = meta:
    if meta ? tags && lib.elem "transient" meta.tags then "dnc" else "cnd";

  mkBootOrderFix = name: meta:
    let order = bootOrderFor meta;
    in {
      triggers_replace = [ "\${xenorchestra_vm.${tfName name}.id}" order ];
      provisioner = [{
        local-exec = {
          # `nix shell nixpkgs#…` (flake registry), NOT `nix-shell -p` —
          # the latter needs a channels NIX_PATH, which flakes-only
          # operator hosts don't have (all 13 hooks failed with "file
          # 'nixpkgs' was not found in the Nix search path", INFRA-234).
          command = ''nix shell nixpkgs#nodejs --command npx --yes xo-cli vm.setBootOrder vm=''${xenorchestra_vm.${tfName name}.id} order=${order}'';
        };
      }];
    };

  # ── Data-source emitters ───────────────────────────────────────
  # XCP-ng primitives are pre-existing; never created via Terraform.
  # Each data source MUST set `provider = "xenorchestra.<instance>"`
  # so Tofu binds it to the aliased provider config (url/token from
  # SOPS) instead of falling back to an unconfigured default provider.
  mkXoPool     = name: meta: { provider = providerAlias meta; name_label = meta.name_label; };
  mkXoNetwork  = name: meta: { provider = providerAlias meta; name_label = meta.name_label; };
  mkXoSr       = name: meta: { provider = providerAlias meta; name_label = meta.name_label; };
  mkXoTemplate = name: meta: { provider = providerAlias meta; name_label = meta.name_label; };
  # ISOs (and any other VDI on an SR — kernels, swaps, scratch images)
  # resolve to data.xenorchestra_vdi entries. The ISO must already
  # exist on the SR (uploaded via xo-cli disk.import url=… or the NFS
  # ISO Library SR pulled from upstream); terranix only references it.
  #
  # pool_id is required: name_label alone is ambiguous (same ISO could
  # live on multiple pools), and the provider's VDI listing endpoint
  # refuses to disambiguate without it. We assume one pool per
  # provider instance (true today — xen-orchestra.main → nj-prod-server-1)
  # and hardcode the reference to xo-pool-main.
  mkXoIso      = name: meta: {
    provider   = providerAlias meta;
    name_label = meta.name_label;
    pool_id    = "\${data.xenorchestra_pool.xo-pool-main.id}";
  };
  inherit tfName;
}
