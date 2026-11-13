# pulumi.nix cases for tests/pulumi_nix.sh: the tf-mini model's stack plus one
# hand-written resource whose properties come from the case.
props:
{ fleetkit, ... }:
{
  imports = [ (fleetkit.lib.pulumi.fromModel { estate = "mini"; }) ];
  stacks.mini-guests.resources.extra-pool = {
    type = "proxmox:index/virtualEnvironmentPool:VirtualEnvironmentPool";
    properties = props;
    options.provider = "\${provider-proxmox}";
  };
}
