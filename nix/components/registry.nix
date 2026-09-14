# nix/components/registry.nix — the component catalog.
#
# Every componentized leaf's `mkComponent` descriptor, listed EXPLICITLY (no
# readDir — matches fleetkit's hand-listed aggregation style and keeps the
# "untracked files invisible to flake eval" gotcha honest). Grouped by
# family; empty groups are valid. Grows as leaves are componentized (M1+).
{ lib }:

let
  mkComponent = import ../lib/mkComponent.nix { inherit lib; };
in
{
  modules =
    let
      # A gated leaf whose interface is locked as-is (no options/ split): the
      # schema buckets every option path declared anywhere under `src`.
      leaf = name: src: mkComponent { family = "module"; inherit name src; requires = [ ]; };
    in
    [
      # dns is the pilot with an explicit options/ split (crisp boundary).
      (mkComponent {
        family = "module";
        name = "infra.network.dns";
        src = ../modules/infra/network/dns;
        requires = [ "options" ];
      })

      # ── strata that own several namespaces across sub-dirs: registered at
      # stratum granularity so each component's src is non-overlapping (a
      # component rooted at a parent would double-bucket its children). ──
      (leaf "infra.base" ../modules/infra/base) # githubAccessToken, networking, nix.gc, platform.*
      (leaf "infra.build" ../modules/infra/build) # hydra, attic, builder, aptCache, remote, …
      (leaf "infra.integrations" ../modules/infra/integrations) # argocd, docker

      # ── clean per-leaf components ──
      (leaf "infra.auth.sssd" ../modules/infra/auth/sssd)
      (leaf "infra.data.pgbouncer" ../modules/infra/data/pgbouncer)
      (leaf "infra.data.pgweb" ../modules/infra/data/pgweb)
      (leaf "infra.data.postgresql" ../modules/infra/data/postgresql) # multi-file (., .backup, .pgwebAccess)
      (leaf "infra.data.rabbitmq" ../modules/infra/data/rabbitmq)
      (leaf "infra.data.s3" ../modules/infra/data/s3)
      (leaf "infra.data.valkey" ../modules/infra/data/valkey)
      (leaf "infra.ingress" ../modules/infra/ingress)
      (leaf "infra.mail.internal" ../modules/infra/mail/internal)
      (leaf "infra.mail.protonmailBridge" ../modules/infra/mail/protonmail-bridge)
      (leaf "infra.network.dhcp" ../modules/infra/network/dhcp)
      (leaf "infra.network.tailnet" ../modules/infra/network/tailnet)
      (leaf "infra.observability.alerts" ../modules/infra/observability/alerts)
      (leaf "infra.observability.alloy" ../modules/infra/observability/alloy)
      (leaf "infra.observability.stack" ../modules/infra/observability/stack)
      (leaf "infra.observability.tempo" ../modules/infra/observability/tempo)
      (leaf "infra.pki.acmeDns" ../modules/infra/pki/acme-dns)
      (leaf "infra.pki.ca" ../modules/infra/pki/ca)
      (leaf "infra.provisioning.pveInstallerAnswers" ../modules/infra/provisioning/pve-installer-answers)
    ];
  # NOTE: infra.services is declared at the infra root (infra/default.nix), not
  # a leaf — a component rooted there would capture the whole tree, so it is
  # intentionally not registered.
  # tf emitters: registered so the family is represented and a
  # component-tf-registered gate catches a deleted/renamed emitter. The
  # RENDERED resource surface is locked by the existing compute-surface-golden
  # check (byte-exact for the proxmox path the fixture exercises); a per-emitter
  # STRUCTURAL surface gate (attr-key shape, refactor-tolerant, covering the
  # xen/cloudflare/grafana emitters) is a follow-up needing broader fixtures.
  tf =
    let
      tfLeaf = name: src: mkComponent { family = "tf"; inherit name src; };
    in
    [
      (tfLeaf "tf.compute.proxmox" ../tf/compute/proxmox.nix)
      (tfLeaf "tf.compute.xen-orchestra" ../tf/compute/xen-orchestra.nix)
      (tfLeaf "tf.compute.ansible" ../tf/compute/ansible.nix)
      (tfLeaf "tf.resources.proxmox" ../tf/resources/proxmox.nix)
      (tfLeaf "tf.resources.xen-orchestra" ../tf/resources/xen-orchestra.nix)
      (tfLeaf "tf.resources.cloudflare" ../tf/resources/cloudflare.nix)
      (tfLeaf "tf.resources.grafana" ../tf/resources/grafana.nix)
    ];

  images = [ ]; # M4
}
