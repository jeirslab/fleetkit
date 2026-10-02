# The `infra` module — fleetkit's entire NixOS surface.
#
# base/ is always-on; everything else is gated by mkEnableOption and
# remains inert unless explicitly enabled in a host definition. File
# tree mirrors the option namespace: the module for
# `infra.<stratum>.<module>` lives at ./<stratum>/<module>, never
# deeper — sub-features hang off their module's option tree (e.g.
# `infra.data.postgresql.backup`), not off a third stratum level.
{ ... }:
{
  imports = [
    # Always-on foundation: core (slim NixOS base — users, ssh, locale
    # UTC, single-DHCP eth0, nix gc; transitively imports platform) +
    # fleet-member.nix (fleet networking, internal CA, builder cache,
    # alloy, SOPS scaffold).
    ./base

    # Fleet-wide service registry (infra.services).
    ./services.nix

    # Compatibility shims: every pre-strata option path (infra.caddy,
    # infra.sssd, infra.builder.*, …) is aliased onto its new home and

    # ── infra.network — fleet-facing network services ──
    ./network/dns # CoreDNS internal zone (infra.network.dns)
    ./network/dhcp # DHCP server (infra.network.dhcp)
    ./network/tailnet # tailnet membership + serve UI (infra.network.tailnet)

    # ── infra.ingress — the fleet reverse proxy (Caddy) ──
    ./ingress

    # ── infra.pki — certificates ──
    ./pki/ca # step-ca internal CA (infra.pki.ca)
    ./pki/host-cert # per-host internal cert (infra.pki.hostCert — on by default)
    ./pki/acme-dns # DNS-01 delegation server (infra.pki.acmeDns — de-sprawl the DNS API token)

    # ── infra.observability ──
    ./observability/stack # Grafana + Prometheus + Loki (infra.observability.stack)
    ./observability/alloy # per-host telemetry agent (infra.observability.alloy)
    ./observability/tempo # Tempo trace store (infra.observability.tempo)
    ./observability/alerts # fleet alert aggregation (rules live beside the modules that own them)

    # ── infra.data — stateful backends ──
    ./data/postgresql # + sub-features .backup / .pgwebAccess
    ./data/postgresql/stale-lockfile.nix
    ./data/pgbouncer # opt-in co-located connection pooler (infra.data.pgbouncer)
    ./data/pgweb # read-only Postgres web UI, fleet-wide bookmarks (infra.data.pgweb)
    ./data/valkey
    ./data/rabbitmq
    ./data/s3 # Garage layout + bucket bootstrap (infra.data.s3)

    # ── infra.build — build/cache infrastructure ──
    ./build/builder # Nix remote builder + harmonia cache (infra.build.builder)
    ./build/remote # client half — offload builds to them (infra.build.remote)
    ./build/attic # attic binary cache (infra.build.attic)
    ./build/hydra.nix
    ./build/lxc-template-factory.nix # infra.build.lxcTemplateFactory
    ./build/deploy-runner.nix # infra.build.deployRunner (GitOps CD, poll)
    ./build/github-runner.nix # infra.build.githubRunner (GitOps CD, push)
    ./build/ci-env.nix # infra.build.ciEnv (the CI environment profile: runner host, act image, agent box)
    ./build/wiki-publisher.nix # infra.build.wikiPublisher
    ./build/registry-proxy.nix # infra.build.registryProxy
    ./build/apt-cache # apt-cacher-ng (infra.build.aptCache)

    # ── infra.mail — mail transport ──
    ./mail/protonmail-bridge # headless Proton Mail Bridge (infra.mail.protonmailBridge)
    ./mail/internal # internal-only Postfix + Dovecot (infra.mail.internal)

    # ── infra.auth — directory / identity ──
    ./auth/sssd # LDAP directory auth; probe sub-feature at infra.auth.sssd.probe

    # ── infra.provisioning ──
    ./provisioning/pve-installer-answers # answer-file HTTP server (infra.provisioning.pveInstallerAnswers)

    # ── infra.integrations — third-party glue ──
    ./integrations/argocd.nix
    ./integrations/docker.nix
  ];
}
