{ config, lib, stackId ? null, ... }:

# XCP-ng / XOA compute emitter. For each fleet.compute entry whose
# provider_instance starts with "xen-orchestra.", emits a paired
# xenorchestra_vm + xenorchestra_cloud_config. The cloud-init body
# comes from nix/lib/cloud-init.nix via the helpers in
# nix/lib/tf/xen-orchestra.nix.

let
  helpers = import ../../lib/tf/xen-orchestra.nix { inherit config lib; };
  sopsLib = import ../../lib/tf/sops.nix { inherit lib; };

  vmsInStack = lib.filterAttrs
    (_: c: (c.enabled or true)
           && "${c.env}.${c.stack}" == stackId
           && c.kind == "vm"
           && lib.hasPrefix "xen-orchestra." c.provider_instance)
    config.fleet.compute;

  # Only VMs that explicitly declare cloud_init.users get a paired
  # xenorchestra_cloud_config. Tier-0 NixOS VMs skip this — their
  # bootstrap template already ships with the sysadmin key on root,
  # and Colmena's first push materialises the rest.
  cloudCfgVms = lib.filterAttrs (_: c: helpers.wantsCloudConfig c) vmsInStack;

  # VyOS-bootstrap secrets get a top-level Terraform `local`. The
  # cloud-init YAML references `${local.vyos_api_token}` — a
  # single-identifier expression that survives the JSON-encoding
  # round trips. Embedding `${data.sops_file.secrets.data["..."]}`
  # directly works at the outer-attribute level (e.g. provider
  # blocks), but inside a YAML scalar that's itself JSON-encoded as
  # the `template` value, the bracket+escaped-quote form picks up an
  # extra `\` layer and Tofu's HCL template parser rejects it.
  hasVyosVm = lib.any
    (c: (c.cloud_init.vyos_config_commands or []) != [])
    (lib.attrValues vmsInStack);

in {
  config = lib.mkIf (stackId != null && vmsInStack != {}) (lib.mkMerge [
    {
      # Resource names from keys go through tfName (nix/lib/tf/name.nix).
      resource.xenorchestra_vm = lib.mapAttrs' (n: c: lib.nameValuePair (helpers.tfName n) (helpers.mkXoVm n c)) vmsInStack;
      # Sibling terraform_data per VM that runs `xo-cli vm.setBootOrder`
      # post-create, enforcing the two-profile boot protocol (INFRA-39):
      # persistent=cnd, transient=dnc. Works around the provider not
      # exposing boot_order on xenorchestra_vm — see comments in
      # nix/lib/tf/xen-orchestra.nix:mkBootOrderFix.
      resource.terraform_data =
        lib.mapAttrs' (n: c: lib.nameValuePair "${helpers.tfName n}-boot-order" (helpers.mkBootOrderFix n c))
          vmsInStack;
    }
    (lib.mkIf (cloudCfgVms != {}) {
      resource.xenorchestra_cloud_config =
        lib.mapAttrs' (n: c: lib.nameValuePair (helpers.tfName n) (helpers.mkXoCloudConfig n c)) cloudCfgVms;
    })
    (lib.mkIf hasVyosVm {
      locals.vyos_api_token = sopsLib.sopsRef "integrations/vyos/api_token";
    })
  ]);
}
