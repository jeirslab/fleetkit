# Flake checks — fleetkit's acceptance gates. `nix flake check` builds all
# of them; CI should treat them as the merge gate.
#
# Philosophy: every check here proves an end-to-end contract, not a unit.
#   * example-fleet   — the parameter surface is COMPLETE: mkFleet over the
#                       template manifest (documentation-range values only)
#                       assembles a deployable host: fleet eval → hostsJson,
#                       stack IDs, and a full NixOS toplevel derivation.
#                       If a module grows a new required setting and the
#                       template isn't taught about it, this fails.
#   * example-tf-render — the terranix pipeline renders the example fleet's
#                       stacks to valid Terraform JSON without eval errors.
#   * mail-internal   — infra.mail.internal instantiates AND still refuses
#                       to relay: the rendered main.cf is asserted, not
#                       just evaluated. Instantiated twice, on Dovecot 2.3
#                       and 2.4, with each version's own `doveconf` parsing
#                       the config rendered for it — the settings option is
#                       freeform, so only the real parser can tell a valid
#                       config from a well-typed one.
#   * launcher        — the fleet CLI package builds and its module tree
#                       imports (catches broken imports/renames that pure
#                       eval never touches).
#   * docs            — the options documentation site builds with
#                       warningsAreErrors: every option fleetkit declares
#                       carries a description, forever.
#   * ansible-syntax   — every framework playbook passes
#                       `ansible-playbook --syntax-check` against the role
#                       tree (catches a broken task file or an undefined
#                       role before a hypervisor run does).
#   * compute-surface-golden — the Terraform JSON rendered for a fixture
#                       fleet that hits every LXC/VM emitter path
#                       (nix/checks/fixtures/compute-surface/) is
#                       byte-identical to the committed goldens
#                       (nix/checks/golden/compute-surface/). A schema or
#                       emitter change must come with an intentional
#                       golden update: nix/checks/update-golden.sh.

{ nixpkgs, mkFleet, sops-nix, disko }:

let
  pkgs = import nixpkgs { system = "x86_64-linux"; };

  # A second fleet namespace on the same estate (ADR-097): synthetic
  # tenant sharing the example's provider instance. NOT in the template
  # (a fresh consumer starts single-fleet); declared inline so the check
  # suite exercises the multi-fleet lift end to end.
  tenantModule = {
    config.fleet.fleets.tenant2 = {
      description = "synthetic second fleet for the check suite";
      providers.proxmox.main.nodes.pve1.resources.lxc.tenant2-app = {
        env = "dev"; stack = "core";
        vm_id = 9102;
        tags = [ "tenant2" ];
        ip = ""; internal_ip = "192.0.2.202";
        cpu_cores = 1; memory_mb = 256; swap_mb = 0;
        root_disk_datastore = "local-lvm";
        network_mode = "single-internal";
        notes = "check-suite tenant machine";
      };
    };
  };

  # Two hosts running infra.mail.internal. Inline here rather than in the
  # template for the same reason as tenantModule: a fresh consumer does
  # not start with a mail server. They exist so the mail module is
  # INSTANTIATED — `nix flake check` never touches a module that no host
  # enables, which is how the Proton Bridge module shipped broken twice.
  #
  # There are two because Dovecot's config language is not one language.
  # nixpkgs selects the package from `system.stateVersion` (dovecot_2_3
  # below 26.05, 2.4 at or above), so which dialect infra.mail.internal
  # must emit is the consumer's choice, not fleetkit's, and both are
  # live: every consumer today lands on 2.3, and the first stateVersion
  # bump lands on 2.4 with no other change. A single host would leave
  # whichever dialect it did not exercise to be discovered by a consumer.
  mailCompute = vmId: ip: {
    env = "platform"; stack = "core";
    provider_instance = "proxmox.main";
    kind = "container";
    vm_id = vmId;
    node = "pve1";
    tags = [ "mail" ];
    ip = ""; internal_ip = ip;
    cpu_cores = 1; memory_mb = 512; swap_mb = 0;
    root_disk_datastore = "local-lvm";
    network_mode = "single-internal";
    notes = "check-suite mail host";
  };

  # No `tls`: the certificate-free path is the one with a promise to
  # keep (plaintext IMAP must be stated outright, not inherited), and it
  # is the one a consumer without a CA actually runs.
  mailHostConfig = ip: {
    infra.networking.singleInterface = true;
    infra.mail.internal = {
      enable = true;
      domain = "mail.example.com";
      hostname = "mx.example.com";
      listenAddress = ip;
      trustedNetworks = [ "127.0.0.0/8" "::1" "192.0.2.0/24" ];
      mailboxes = {
        "alerts@mail.example.com".passwordFile = "/run/secrets/mail-alerts";
        "reports@mail.example.com".passwordFile = "/run/secrets/mail-reports";
      };
    };
  };

  mailModule = {
    config.fleet.compute.example-mail = mailCompute 102 "192.0.2.102";
    config.fleet.compute.example-mail-24 = mailCompute 103 "192.0.2.103";

    config.fleet.hostsRegistry.example-mail = { ... }:
      mailHostConfig "192.0.2.102";

    # Pinned forward rather than raising stateVersion: stateVersion moves
    # more than Dovecot, and this host exists to vary one thing.
    config.fleet.hostsRegistry.example-mail-24 = { pkgs, ... }:
      mailHostConfig "192.0.2.103" // {
        services.dovecot2.package = pkgs.dovecot;
      };
  };

  example = mkFleet {
    modules = [ ../templates/minimal/fleet tenantModule mailModule ];
    # No backend argument — deliberately: proves the ADR-097 fallback to
    # fleet.settings.backend (the template declares the bucket there).
  };

  mailHost = example.nixosConfigurations.example-mail.config;
  mailHost24 = example.nixosConfigurations.example-mail-24.config;

  # Negative test: the SAME resource name in two fleet namespaces must be
  # a hard eval error (names are estate-global). tryEval + deepSeq —
  # nothing short of forcing the value would trip it (the four
  # green-check-over-broken-code lessons).
  collisionEval = nixpkgs.lib.evalModules {
    modules = [
      ./fleet
      { _module.args.fleetLib =
          import ./lib/module-args.nix { lib = nixpkgs.lib; inherit pkgs; }; }
      ../templates/minimal/fleet tenantModule {
      config.fleet.fleets.rogue.providers.proxmox.main.nodes.pve1.resources.lxc.tenant2-app = {
        env = "dev"; stack = "core"; vm_id = 9103;
        ip = ""; internal_ip = "192.0.2.203";
      };
    } ];
  };
  collisionCaught =
    !(builtins.tryEval
        (builtins.deepSeq collisionEval.config.fleet.compute true)).success;

  fleetPkg = pkgs.callPackage ./pkgs/_launcher {
    xoa-cli = pkgs.callPackage ./pkgs/xoa-cli { };
  };

  golden = mkFleet {
    modules = [ ./checks/fixtures/compute-surface ];
    backend = { bucket = "golden-tofu"; };
  };
  goldenRenders = pkgs.lib.mapAttrs' (n: v: pkgs.lib.nameValuePair (pkgs.lib.removePrefix "tf-" n) v)
    (pkgs.lib.filterAttrs (n: _: pkgs.lib.hasPrefix "tf-" n && n != "tf-stack-ids") golden.packages);
  goldenDir = ./checks/golden/compute-surface;

in {
  # The toplevel drvPath goes through unsafeDiscardOutputDependency: a
  # bare `.drvPath` string carries a "build all outputs" context, which
  # turned this eval gate into a full build of the example NixOS closure
  # (tens of GiB). Instantiating the derivation is what proves the module
  # stack closes; building it proves nothing more about the schema.
  example-fleet = pkgs.runCommand "fleetkit-example-check" {
    hostsJson = example.packages.hostsJson;
    exampleToplevelDrv = builtins.unsafeDiscardOutputDependency
      example.nixosConfigurations.example.config.system.build.toplevel.drvPath;
    stackIds = example.packages.tf-stack-ids;
    # v2 path forced end-to-end (ADR-096): the v2-authored host's closure,
    # plus eval-time assertions on the lift. Four times on the consumer
    # port a green check sat over broken code because nothing FORCED the
    # value — these are all strict env attrs, so instantiating this
    # derivation forces every one.
    exampleV2ToplevelDrv = builtins.unsafeDiscardOutputDependency
      example.nixosConfigurations.example-v2.config.system.build.toplevel.drvPath;
    v2Lift = builtins.toJSON {
      kind = example.fleetEval.compute.example-v2.kind;                      # "container"
      node = example.fleetEval.compute.example-v2.node;                      # "pve1"
      pi   = example.fleetEval.compute.example-v2.provider_instance;         # "proxmox.main"
      secretHosts = example.fleetEval.secrets.example-v2.consumers.hosts;    # [ "example-v2" ]
      sopsKey = example.nixosConfigurations.example-v2.config
        .sops.secrets."example-v2/default/api_token".key;                    # "default/api_token"
    };
    # Multi-fleet lift (ADR-097): the tenant machine lands in the flat
    # layer tagged with its namespace, its stack enumerates
    # fleet-prefixed (⇒ namespaced state key), and a cross-namespace
    # name collision is a hard eval error.
    fleetsLift = builtins.toJSON {
      tenantNs    = example.fleetEval.compute.tenant2-app.fleet_ns;   # "tenant2"
      tenantScope = example.fleetEval.compute.tenant2-app.scope;      # "fleet" (default)
      tenantStack = builtins.hasAttr "tenant2.dev.core" example.fleetEval.stacks;
      incumbentUnprefixed = builtins.hasAttr "platform.core" example.fleetEval.stacks;
      inherit collisionCaught;
    };

    # The CLI catalog (ADR-097): force it as a strict env attr and assert
    # the settings→catalog projection (bucket via the settings fallback,
    # toml-era dotted keys present).
    catalog = builtins.readFile "${example.packages.fleet-catalog}";
    passAsFile = [ "v2Lift" "catalog" "fleetsLift" ];
  } ''
    test -s "$hostsJson"
    test -s "$stackIds"
    echo "example toplevel: $exampleToplevelDrv"
    echo "v2 toplevel:      $exampleV2ToplevelDrv"
    grep -q '"kind":"container"' "$v2LiftPath"
    grep -q '"node":"pve1"' "$v2LiftPath"
    grep -q '"sopsKey":"default/api_token"' "$v2LiftPath"
    grep -q '"secretHosts":\["example-v2"\]' "$v2LiftPath"
    grep -q '"bucket":"REPLACE-ME-tofu"' "$catalogPath"
    grep -q '"extensions_dir":"cli-ext"' "$catalogPath"
    grep -q '"secrets_file":"nix/secrets/secrets.yaml"' "$catalogPath"
    grep -q '"tenantNs":"tenant2"' "$fleetsLiftPath"
    grep -q '"tenantScope":"fleet"' "$fleetsLiftPath"
    grep -q '"tenantStack":true' "$fleetsLiftPath"
    grep -q '"incumbentUnprefixed":true' "$fleetsLiftPath"
    grep -q '"collisionCaught":true' "$fleetsLiftPath"
    touch $out
  '';

  # Render every leaf stack of the example fleet to Terraform JSON and
  # sanity-parse it. Catches emitter regressions that host eval misses
  # (the tf-<slug> packages are only built on demand otherwise).
  example-tf-render = pkgs.runCommand "fleetkit-example-tf-render" {
    nativeBuildInputs = [ pkgs.jq ];
    renders = pkgs.lib.attrValues
      (pkgs.lib.filterAttrs (n: _: pkgs.lib.hasPrefix "tf-" n && n != "tf-stack-ids")
        example.packages);
  } ''
    for r in $renders; do
      jq -e 'has("resource") or has("data") or has("provider")' "$r" > /dev/null \
        || { echo "render $r is not Terraform JSON"; exit 1; }
    done
    touch $out
  '';

  # infra.mail.internal keeps its one promise: no route off the domain.
  #
  # Forcing the mail toplevels (below, as strict env attrs) proves the
  # module stack closes. That is necessary and not sufficient — a green
  # eval says nothing about what Postfix will actually read. So this
  # reads the RENDERED files, not the option values: main.cf (reached
  # through the postfix-setup script that symlinks it, since the nixpkgs
  # module keeps it let-bound) and both dovecot.conf files. Checking
  # settings.main instead would pass while a later mkForce elsewhere
  # rewrote the file.
  #
  # And for Dovecot, rendering is still not enough either: the settings
  # option is freeform, so a setting that the running Dovecot has never
  # heard of evaluates, renders, and is a fatal parse error at start.
  # That is not hypothetical — the 2.3 spelling of this module rendered
  # and shipped green while asserting in every consumer. So each host's
  # own `doveconf` parses its own file. A dialect mismatch is a build
  # failure here rather than a dead unit on a host.
  mail-internal = pkgs.runCommand "fleetkit-mail-internal-check" {
    mailToplevelDrv = builtins.unsafeDiscardOutputDependency
      mailHost.system.build.toplevel.drvPath;
    mail24ToplevelDrv = builtins.unsafeDiscardOutputDependency
      mailHost24.system.build.toplevel.drvPath;
    # ExecStart is "<script> " — the unit has no arguments, but the
    # systemd module still joins on a space. removeSuffix keeps the
    # string context (splitString would drop it, and the script would
    # then not be an input to this derivation at all).
    setupScript = pkgs.lib.removeSuffix " "
      mailHost.systemd.services.postfix-setup.serviceConfig.ExecStart;
    dovecotConf = mailHost.services.dovecot2.configFile;
    dovecotConf24 = mailHost24.services.dovecot2.configFile;
    doveconf = pkgs.lib.getExe' mailHost.services.dovecot2.package "doveconf";
    doveconf24 = pkgs.lib.getExe' mailHost24.services.dovecot2.package "doveconf";
  } ''
    echo "mail toplevel:     $mailToplevelDrv"
    echo "mail 2.4 toplevel: $mail24ToplevelDrv"

    mainCf=$(grep -o '/nix/store/[^ ]*-postfix-main\.cf' "$setupScript" | head -n1)
    test -n "$mainCf" || { echo "could not find main.cf in $setupScript"; exit 1; }
    echo "main.cf: $mainCf"

    # An empty list renders as the key, then a whitespace-only
    # continuation line — so these are exact-line matches on purpose.
    grep -qx 'relayhost =' "$mainCf"
    grep -qx 'relay_domains =' "$mainCf"
    grep -qx 'mydestination =' "$mainCf"

    # Accepted mail goes to Dovecot, for the one domain, and nowhere else.
    grep -qx 'virtual_transport = lmtp:unix:private/dovecot-lmtp' "$mainCf"
    grep -q 'virtual_mailbox_domains' "$mainCf"
    grep -q 'mail.example.com' "$mainCf"

    # The relay restrictions are a multi-line logical entry: pull just
    # that entry out (the key line plus its indented continuations) so
    # permit_mynetworks in smtpd_client_restrictions — where it belongs —
    # does not mask its return here, where it must never appear.
    relay=$(awk '
      /^smtpd_relay_restrictions =/ { inentry = 1; print; next }
      inentry && /^[[:space:]]/     { print; next }
      inentry                       { exit }
    ' "$mainCf")
    echo "smtpd_relay_restrictions: $relay"
    case "$relay" in
      *reject_unauth_destination*) ;;
      *) echo "smtpd_relay_restrictions lost reject_unauth_destination"; exit 1 ;;
    esac
    case "$relay" in
      *permit_mynetworks*)
        echo "smtpd_relay_restrictions grew permit_mynetworks — this host can relay off-domain"
        exit 1 ;;
    esac

    # Hand each rendered file to the Dovecot that will read it.
    #
    # doveconf resolves default_login_user and default_internal_user
    # against the passwd database and refuses a config naming a user that
    # does not exist. The build sandbox has neither dovenull nor dovecot2,
    # so both are redefined at the end of a copy — a later definition
    # wins — which leaves every other line to be parsed exactly as the
    # host will get it.
    validate() {
      local tool="$1" conf="$2" label="$3"
      { cat "$conf"
        # doveConf does not end in a newline; without this the first
        # override would be glued onto the last rendered line.
        echo
        echo 'default_login_user = nobody'
        echo 'default_internal_user = nobody'
      } > "$label.conf"
      echo "=== $label ($tool) ==="
      if ! "$tool" -c "$label.conf" -n > "$label.parsed"; then
        echo "$label: dovecot refused to parse the config this module rendered for it"
        cat "$conf"
        exit 1
      fi
    }
    validate "$doveconf"   "$dovecotConf"   dovecot-2.3
    validate "$doveconf24" "$dovecotConf24" dovecot-2.4

    # Dovecot authenticates against the runtime passwd file and not PAM,
    # and hands Postfix an LMTP socket inside the queue directory. The
    # passdb spelling is version-specific; the socket is not.
    for c in "$dovecotConf" "$dovecotConf24"; do
      grep -q '/run/mail-internal/passwd' "$c"
      grep -q 'unix_listener /var/lib/postfix/queue/private/dovecot-lmtp' "$c"
      if grep -qE 'driver = pam|^passdb pam ' "$c"; then
        echo "dovecot kept the PAM passdb — a shell account could log in as mail"
        exit 1
      fi
    done
    grep -q 'driver = passwd-file' "$dovecotConf"
    grep -q 'passwd_file_path = /run/mail-internal/passwd' "$dovecotConf24"

    # With no certificate, serving IMAP means saying so. nixpkgs used to
    # emit both of these on the module's behalf and no longer does, which
    # is how "this module does not restate that; it just inherits it"
    # became a comment describing a login path that refuses every login.
    grep -qx 'ssl = no' "$dovecotConf"
    grep -qx 'ssl = no' "$dovecotConf24"
    grep -qx 'disable_plaintext_auth = no' "$dovecotConf"
    grep -qx 'auth_allow_cleartext = yes' "$dovecotConf24"

    touch $out
  '';

  # The launcher package builds and every module imports.
  launcher = pkgs.runCommand "fleetkit-launcher-check" {
    nativeBuildInputs = [ fleetPkg ];
  } ''
    fleet --help > /dev/null
    touch $out
  '';

  # The CLI verb surface is locked: `fleet --dump-verbs` (a sandbox-safe
  # introspection that touches no env/SOPS) prints the framework command tree,
  # diffed against a committed golden. A renamed/removed/added verb — including
  # as component families fold their CLIs upward — fails here until the golden
  # is refreshed. Structural (verb paths only), not help text, to avoid churn.
  cli-verbs-golden = pkgs.runCommand "fleetkit-cli-verbs-golden" {
    nativeBuildInputs = [ fleetPkg pkgs.jq pkgs.diffutils ];
    golden = ./checks/golden/cli-verbs.json;
  } ''
    fleet --dump-verbs > verbs.json
    diff -u <(jq -S . "$golden") <(jq -S . verbs.json) \
      || { echo "CLI verb surface changed — if intended, run nix/checks/update-cli-verbs.sh"; exit 1; }
    touch $out
  '';

  # The AI-agent discovery surface assembles end-to-end: `fleet describe`
  # emits one JSON manifest carrying the command tree, the option surface,
  # and the component interfaces. Gates the contract the `introspection`
  # package (and any importing agent) relies on — not a byte-golden (the
  # option/verb goldens already lock the content), just that the surface is
  # whole and self-describing.
  introspection-surface = pkgs.runCommand "fleetkit-introspection-surface" {
    nativeBuildInputs = [ fleetPkg pkgs.jq ];
    FLEET_OPTIONS_JSON = (import ../docs { inherit pkgs nixpkgs sops-nix disko; }).passthru.optionsJSON;
    FLEET_COMPONENTS_DIR = ./components/schema;
  } ''
    fleet describe > m.json
    jq -e '.commands | length > 0'                 m.json >/dev/null || { echo "describe: no commands";              exit 1; }
    jq -e '.commands[] | select(.path=="describe")' m.json >/dev/null || { echo "describe: not self-listed";          exit 1; }
    jq -e 'has("options")'                          m.json >/dev/null || { echo "describe: missing option surface";    exit 1; }
    jq -e '.components | has("modules") and has("images")' m.json >/dev/null || { echo "describe: missing components"; exit 1; }
    touch $out
  '';

  # Framework playbooks parse and their roles resolve. Syntax-only: no
  # host is contacted (`-i localhost,` satisfies inventory loading).
  ansible-syntax = pkgs.runCommand "fleetkit-ansible-syntax-check" {
    nativeBuildInputs = [ pkgs.ansible ];
    src = ../ansible;
  } ''
    export HOME=$TMPDIR ANSIBLE_LOCAL_TEMP=$TMPDIR ANSIBLE_ROLES_PATH=$src/roles
    printf '[defaults]\nhost_key_checking = False\n' > $TMPDIR/ansible.cfg
    export ANSIBLE_CONFIG=$TMPDIR/ansible.cfg
    for pb in pve pbs developer site; do
      ansible-playbook --syntax-check -i localhost, "$src/playbooks/$pb.yml"
    done
    touch $out
  '';

  # The options documentation site builds. nixosOptionsDoc runs with
  # warningsAreErrors = true, so any fleetkit option without a
  # description fails this check.
  docs = import ../docs { inherit pkgs nixpkgs sops-nix disko; };

  compute-surface-golden = (pkgs.runCommand "fleetkit-compute-surface-golden" {
    nativeBuildInputs = [ pkgs.jq pkgs.diffutils ];
    slugs = pkgs.lib.attrNames goldenRenders;
    renderPaths = pkgs.lib.attrValues goldenRenders;
    inherit goldenDir;
    passthru = {
      renders = goldenRenders;
      slugs = pkgs.lib.concatStringsSep " " (pkgs.lib.attrNames goldenRenders);
    };
  } ''
    set -- $renderPaths
    fail=0
    for slug in $slugs; do
      render=$1; shift
      golden="$goldenDir/$slug.json"
      if [ ! -f "$golden" ]; then
        echo "missing golden $slug.json — run nix/checks/update-golden.sh"; fail=1; continue
      fi
      if ! diff -u <(jq -S . "$golden") <(jq -S . "$render"); then
        echo "render of stack $slug differs from golden — if intended, run nix/checks/update-golden.sh"; fail=1
      fi
    done
    [ "$fail" = 0 ] || exit 1
    touch $out
  '');
}
