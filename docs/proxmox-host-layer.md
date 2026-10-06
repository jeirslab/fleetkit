# Proxmox host layer: provider first, scripts only for the remainder

Principle (owner): keep as much as possible in the bpg/proxmox provider (0.115.0, schema in providers/schemas); use host scripts or Ansible only for what the provider cannot express. The community scripts are a reference, not a dependency.

## Covered by the provider (declare in the model, render as tofu resources)
| Task | Provider resources |
|---|---|
| Repositories (no-subscription, enterprise off) | apt_repository, apt_standard_repository |
| HA | hagroup, haresource, harule |
| PCIe / USB passthrough definitions | hardware_mapping_pci, hardware_mapping_usb (+ vm hostpci; containers use device_passthrough) |
| SSO | realm_openid, realm_ldap, realm_sync; users, groups, roles, acls, user_token |
| Certificates / ACME | acme_certificate, acme_dns_plugin, certificate |
| Cluster, DNS, hosts, time | cluster_options, dns, hosts, time |
| Networking | network_linux_bridge/bond/vlan, network_applier, sdn_* (zones, vnets, subnets, fabrics) |
| Firewall | cluster_firewall, node_firewall, firewall_rules/ipset/alias/options, security groups |
| Storage and backup | storage_nfs/zfspool/lvm/lvmthin/directory/cifs/pbs, backup_job, metrics_server, download_file, file, pool |

## NOT expressible in the provider (host baseline; small, per node)
- subscription nag removal (patches a UI file), kernel cleanup, microcode
- IOMMU and vfio host preparation for passthrough (kernel command line, modules), ZFS tuning (ARC limit)
- joining or removing a bare-metal node from a cluster (a one-off runbook step, `pvecm add`)
These become `fleet.sites.<s>.nodes.<n>.host = { ... }` data rendered into a short playbook (or script) view, validated by the same schema. Do not port the community scripts wholesale.

## Dependencies: substrate before consumers
Some stacks configure something that needs another service to exist first (SSO needs the identity server and an OIDC client, ACME needs the CA, backups need PBS). Two different questions:
1. DECLARED (evaluation time): the estate lists a guest for the substrate role and the variables the consumer needs resolve (e.g. identity issuer URL from guest address + domain; ca directory from the acme server block). Fails evaluation if the role is empty or a variable cannot be derived.
2. AVAILABLE (run time): the service is actually up and reachable. That is a CLI preflight, not a schema check.
Ordering follows from stack dependencies (stage 0 hosts and network, stage 1 platform guests: dns, ca, identity, stage 2 consumers: SSO realm, ACME certificates). Keep a break-glass local (pam) realm so SSO can never lock the cluster out.
