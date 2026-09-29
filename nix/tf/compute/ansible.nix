{ config, lib, stackId ? null, ... }:

# ansible_playbook emitter — chains post-install Ansible to the compute
# resource tofu just created. Once a host's VM/LXC exists per fleet.compute,
# the matching `ansible_playbook` resource runs the corresponding playbook
# against it. End result: `fleet deploy tf apply <stack>` brings a host from
# "doesn't exist" to "fully converged" in one command.
#
# Two conventions today:
#   - kind=container with non-NixOS image → playbooks/developer.yml
#     (Debian dev workstations — Docker, Nix, dev tools)
#   - kind=vm with tag "pve-host"         → playbooks/pve.yml
#     (PVE hypervisor — base + proxmox/base + proxmox/pve roles)
#
# Both `ansible_playbook` resources set `replayable = true` so they re-run
# on every `tofu apply` (idempotent at the Ansible-task level — already-
# converged hosts stay no-op). depends_on the corresponding compute
# resource so apply order is deterministic: VM/LXC creation completes
# before ansible-playbook starts.
#
# SSH + auth: ansible-playbook runs locally on whoever runs tofu apply.
# The operator must have:
#   • the sysadmin SSH private key (fleet.network.sysadmin_key_file)
#   • SOPS_AGE_KEY_FILE pointing at the age key (for community.sops
#     lookups in roles like proxmox/pve acme, proxmox/pbs garage-key)
# `fleet deploy tf apply` sets both via the launcher env bootstrap.
#
# Path: the framework playbooks ship with fleetkit itself (../../../
# ansible). Interpolating the whole tree copies it to the Nix store, so
# the playbook path baked into config.tf.json is absolute and the
# sibling roles/ ride along (ansible/playbooks/roles is a symlink to
# ../roles, making the copied tree self-resolving without any
# ANSIBLE_ROLES_PATH). A consumer can substitute its own playbook per
# host via fleet.compute.<name>.ansible_playbook (a string, resolved by
# ansible-playbook relative to the tofu working dir .tf/<stack>/ when
# not absolute).

let
  inherit (lib) filterAttrs mapAttrs' hasInfix elem;

  ansibleTree = ../../../ansible;

  computeInStack = filterAttrs
    (_: c: (c.enabled or true)
           && "${c.env}.${c.stack}" == stackId
           && lib.hasPrefix "proxmox." c.provider_instance)
    config.fleet.compute;

  # Pick the playbook based on the host class. Returns null when no
  # convention matches (NixOS LXCs, etc. — Colmena manages those).
  # A per-host fleet.compute.<name>.ansible_playbook overrides the
  # convention playbook (but never opts extra hosts in).
  conventionPlaybookFor = name: meta:
    if meta.kind == "container" && (meta.image or null) != null then
      "${ansibleTree}/playbooks/developer.yml"
    else if meta.kind == "vm" && elem "pve-host" (meta.tags or []) then
      "${ansibleTree}/playbooks/pve.yml"
    else null;

  playbookFor = name: meta:
    if (conventionPlaybookFor name meta) == null then null
    else if (meta.ansible_playbook or null) != null then meta.ansible_playbook
    else conventionPlaybookFor name meta;

  # Group every Ansible-managed host belongs to so the matching play
  # (`- hosts: developer` / `- hosts: pve`) actually matches. The
  # ansible_host resource the provider emits would otherwise leave the
  # host in no group, and `--limit <host>` wouldn't intersect with
  # `- hosts: developer`.
  groupFor = name: meta:
    if meta.kind == "container" && (meta.image or null) != null then "developer"
    else if meta.kind == "vm" && elem "pve-host" (meta.tags or []) then "pve"
    else null;

  # Anchor the ansible_playbook resource to the compute resource that
  # was just created. Forces apply ordering.
  computeRef = name: meta:
    if meta.kind == "container"
    then "proxmox_virtual_environment_container.${name}"
    else "proxmox_virtual_environment_vm.${name}";

  # Inventory name = the operational hostname (compute `name`, unless it is
  # a dotted legacy override, then the key) — same rule as fleet/default.nix
  # hostLabel. The tofu RESOURCE addresses stay keyed by the fleet key so a
  # CTID-keyed host is `ansible_host."2011"` with inventory name `nithin`:
  # `--limit nithin`, host_vars/nithin.yml and inventory_hostname all read
  # as a machine name, never as a number.
  hostLabel = key: meta:
    if hasInfix "." (meta.name or key) then key else meta.name or key;

  ansibleManagedHosts = filterAttrs
    (name: meta: (playbookFor name meta) != null)
    computeInStack;

  emitHost = name: meta: {
    name = name;
    value = {
      name = hostLabel name meta;
      groups = [ (groupFor name meta) ];
      # Inject the bare minimum host_vars the static inventory provides
      # via group_vars/host_vars. The temp inventory the provider writes
      # to /tmp doesn't read the file-based inventory tree.
      variables = {
        ansible_host = meta.internal_ip;
        ansible_user = "root";
        ansible_ssh_private_key_file = config.fleet.network.sysadmin_key_file;
        ansible_ssh_common_args = "-o StrictHostKeyChecking=accept-new";
        ansible_python_interpreter = "/usr/bin/python3";
      };
    };
  };

  emitPlaybook = name: meta: {
    name = "${name}-ansible";
    value = {
      playbook = playbookFor name meta;
      name = hostLabel name meta;
      groups = [ (groupFor name meta) ];
      replayable = true;
      ignore_playbook_failure = false;
      # depends_on the compute resource AND the host resource, so the
      # temp inventory the provider generates has the right group + vars.
      depends_on = [
        "${computeRef name meta}"
        "ansible_host.${name}"
      ];
    };
  };

  ansibleEnabled = (config.fleet.providers.ansible or null) != null;
in {
  config = lib.mkIf (stackId != null && ansibleEnabled && ansibleManagedHosts != {}) {
    resource.ansible_host = mapAttrs' (n: m: { name = n; value = (emitHost n m).value; }) ansibleManagedHosts;
    resource.ansible_playbook = mapAttrs' emitPlaybook ansibleManagedHosts;
  };
}
