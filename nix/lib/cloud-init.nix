{ config, lib }:

# Per-VM cloud-init snippet renderer. Generic over the substrate —
# Proxmox emits this as a `local:snippets/<name>-user-data.yaml` file
# resource (see fleet/resources.nix); XCP-ng/XOA emits it as a
# `xenorchestra_cloud_config` resource (see lib/tf/xen-orchestra.nix).
#
# The function reads from `config.fleet.network` (sysadmin pubkey + DNS
# domain) and `config.fleet.access.users` (identity registry) — both
# must be in scope on the caller. The VM's per-host attrs come via
# the `vmMeta` argument (a fleet.compute entry).
#
# Extracted out of fleet/resources.nix so the XOA emitter can share the
# renderer without circular imports.

let
  net = config.fleet.network;
  registry = config.fleet.access.users;

  # virtio data disks → /dev/vd{b,c,d,...}.
  diskLetter = i: builtins.elemAt [ "b" "c" "d" "e" "f" "g" ] i;

  # Resolve a cloud_init.users entry to {name, ssh_keys, linux_groups, sudo}.
  # `ref` → registry lookup. Otherwise inline. Linux groups on the
  # VM are derived from `sudo` (adds "sudo") + `extra_groups` —
  # NEVER from registry.groups (those are LDAP groups, different scope).
  resolveUser = u:
    let
      inline = u.ref == null;
      base = if inline
        then { name = u.name; ssh_keys = u.ssh_keys; }
        else (
          if !(registry ? ${u.ref})
          then throw "cloud_init.users[*].ref = \"${u.ref}\" not found in fleet.access.users"
          else { name = u.ref; ssh_keys = registry.${u.ref}.ssh_keys; }
        );
      linuxGroups = lib.unique ((lib.optional u.sudo "sudo") ++ u.extra_groups);
    in base // { inherit linuxGroups; sudo = u.sudo; };
in {
  inherit diskLetter resolveUser;

  renderCloudInitSnippet = vmName: vmMeta:
    let
      ci = vmMeta.cloud_init;
      dataDisks = vmMeta.data_disks;

      # Users block: root with sysadmin key + each declared user (resolved).
      userEntry = u_: let
        u = resolveUser u_;
        keysList = lib.concatMapStringsSep "\n"
          (k: "      - ${k}") u.ssh_keys;
        groupsList = "[${lib.concatStringsSep ", " u.linuxGroups}]";
      in "\n  - name: ${u.name}"
       + "\n    shell: /bin/bash"
       + lib.optionalString u.sudo "\n    sudo: ALL=(ALL) NOPASSWD:ALL"
       + "\n    groups: ${groupsList}"
       + "\n    ssh_authorized_keys:"
       + "\n" + keysList;
      usersBlock = lib.concatMapStrings userEntry ci.users;

      # write_files block — preserved formatting for content (uses YAML | literal).
      writeFileEntry = wf:
        let
          # Indent each content line by 6 spaces so YAML `content: |` reads it as the value.
          indentedContent = lib.concatMapStringsSep "\n"
            (l: "      ${l}") (lib.splitString "\n" wf.content);
        in "\n  - path: ${wf.path}"
         + "\n    permissions: \"${wf.permissions}\""
         + "\n    owner: ${wf.owner}"
         + "\n    content: |\n${indentedContent}";
      writeFilesBlock = lib.optionalString (ci.write_files != []) (
        "\n\nwrite_files:" + lib.concatMapStrings writeFileEntry ci.write_files
      );

      # fs_setup + mounts + resize bootcmd, one per data disk.
      fsSetupBlock = lib.optionalString (dataDisks != []) (
        "\n\nfs_setup:" + lib.concatStrings (lib.imap0 (i: dd:
          let dev = "/dev/vd${diskLetter i}"; label = lib.replaceStrings ["/"] [""] dd.mount_path; in
          "\n  - label: ${label}"
          + "\n    filesystem: ${dd.filesystem}"
          + "\n    device: ${dev}"
          + "\n    overwrite: false"
        ) dataDisks)
      );
      mountsBlock = lib.optionalString (dataDisks != []) (
        "\n\nmounts:" + lib.concatStrings (lib.imap0 (i: dd:
          "\n  - [ /dev/vd${diskLetter i}, ${dd.mount_path}, ${dd.filesystem}, \"defaults,nofail\", \"0\", \"2\" ]"
        ) dataDisks)
      );
      # Idempotent grow: cloud-init's growpart module handles the root
      # partition automatically; bootcmd resize2fs handles each data
      # disk on every boot (no-op when fs is already at device size).
      resizeBootcmds = lib.imap0 (i: dd:
        "  - [ sh, -c, \"resize2fs /dev/vd${diskLetter i} || true\" ]"
      ) dataDisks;

      # Optional Nix install + free-form runcmd passthrough. Each entry
      # is a complete YAML list item at the right indent (2 spaces).
      nixRuncmd = lib.optional ci.install_nix (
        "  - |\n"
        + "    curl --proto '=https' --tlsv1.2 -sSf -L https://install.determinate.systems/nix \\\n"
        + "      | sh -s -- install linux --no-confirm --init systemd \\\n"
        + "          --extra-conf \"extra-substituters = https://nix-community.cachix.org\" \\\n"
        + "          --extra-conf \"extra-trusted-public-keys = nix-community.cachix.org-1:mB9FSh9qf2dCimDSUo8Zy7bkq5CX+/rkCWyvRCYg3Fs=\""
      );
      extraRuncmd = map (l: "  - ${l}") ci.runcmd;
      allRuncmd = nixRuncmd ++ guestAgentRuncmd ++ extraRuncmd;
      runcmdBlock = lib.optionalString (allRuncmd != []) (
        "\n\nruncmd:\n" + lib.concatStringsSep "\n" allRuncmd
      );

      # Mask systemd-firstboot. Debian's genericcloud images ship it
      # enabled and, with an empty /etc/machine-id, it stops sysinit.target
      # on an interactive locale/timezone prompt. bootcmd runs from
      # cloud-init-local, which is ordered before sysinit.target too but
      # not against systemd-firstboot itself, so `--now` also stops a
      # prompt that has already started. Same two commands as the
      # image-prep path (nix/images/debian-cloud); this is the variant for
      # a stock upstream image that was never prepared.
      maskFirstbootBootcmd = lib.optional ci.mask_firstboot
        "  - [ sh, -c, \"ln -sf /dev/null /etc/systemd/system/systemd-firstboot.service; systemctl mask --now systemd-firstboot.service 2>/dev/null || true\" ]";
      extraBootcmd = map (l: "  - ${l}") ci.bootcmd;
      allBootcmd = maskFirstbootBootcmd ++ extraBootcmd ++ resizeBootcmds;
      bootcmdBlock = lib.optionalString (allBootcmd != []) (
        "\n\nbootcmd:\n" + lib.concatStringsSep "\n" allBootcmd
      );

      # QEMU guest agent: a package install needs the network, so it is
      # `packages:` + an enable in runcmd, not bootcmd. PVE's agent=1 on
      # the VM is the other half (vm.agent in the compute schema).
      packagesBlock = lib.optionalString ci.install_guest_agent
        "\n\npackage_update: true\npackages:\n  - qemu-guest-agent";
      guestAgentRuncmd = lib.optional ci.install_guest_agent
        "  - [ systemctl, enable, --now, qemu-guest-agent ]";

      # VyOS-specific cloud-init config-commands block. VyOS's cloud-init
      # module wraps these in a load/set/commit/save transaction; nothing
      # is committed until every command parses successfully. Non-VyOS
      # cloud-init implementations silently ignore the block, so this is
      # safe to emit unconditionally.
      vyosCommandsBlock = lib.optionalString (ci.vyos_config_commands != []) (
        "\n\nvyos_config_commands:\n"
        + lib.concatMapStringsSep "\n"
            (cmd: "  - ${builtins.toJSON cmd}") ci.vyos_config_commands
      );

      # cloud_init.hostname (explicit) > vmMeta.name (the operational
      # hostname, defaulting to the fleet key). The FQDN domain follows the
      # per-guest dns.domain when one is declared, else the fleet-wide one.
      hostname = if ci.hostname != "" then ci.hostname else vmMeta.name;
      fqdnDomain =
        let d = vmMeta.dns.domain or null;
        in if d != null && d != "" then d else net.dns_domain;
      # final_message shows the user to SSH as. For ref-mode, that's
      # the registry key (== Authentik username); for inline, .name.
      primaryUser =
        if ci.users != [] then (resolveUser (lib.head ci.users)).name
        else "root";
      # XCP-ng VMs leave internal_ip empty (DHCP'd via XCP-ng-internal
      # NIC, fleet integration via tailnet). Fall back to "DHCP" in
      # final_message so the YAML is still valid.
      reachAddr = if vmMeta.internal_ip != "" then vmMeta.internal_ip else "the DHCP-assigned address";
    in ''
      #cloud-config
      hostname: ${hostname}
      fqdn: ${hostname}.${fqdnDomain}
      manage_etc_hosts: true

      users:
        - name: root
          ssh_authorized_keys:
            - ${net.sysadmin_ssh_key}${usersBlock}'' + fsSetupBlock + mountsBlock + writeFilesBlock + packagesBlock + bootcmdBlock + runcmdBlock + vyosCommandsBlock + ''


      final_message: "${vmName} first-boot complete in $UPTIME seconds. SSH via ${primaryUser}@${reachAddr}."
    '';
}
