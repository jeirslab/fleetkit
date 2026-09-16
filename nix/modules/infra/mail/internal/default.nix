# Internal-only mail: Postfix for transport, Dovecot for mailboxes.
#
# This is a mail system for services talking to each other inside one
# fleet. It is authoritative for exactly one domain and refuses every
# other destination — there is no relayhost, no smarthost, no fallback
# route to the internet. A service that mistypes a recipient gets a
# rejection during the SMTP conversation rather than a message on its way
# out of the building. That refusal is the feature; if you want mail to
# reach a human, put a gateway in front of this and let that gateway own
# the decision.
#
# Shape:
#
#   postfix  :25 (and :587 when submission.enable) → accepts from
#            trustedNetworks, rejects any recipient outside `domain`,
#            hands the rest to Dovecot over LMTP on a unix socket in
#            Postfix's own queue directory.
#   dovecot  owns the Maildirs and serves IMAP. Accounts are virtual: no
#            system user per mailbox, one uid owns all mail.
#
# Mailbox *names* are declared in Nix and end up in the store — they are
# not secret, and being able to diff them is the point. Only the password
# hashes come from files, which is what lets them live in SOPS.
#
# Five things worth knowing before changing this:
#
#   1. Without a certificate, Dovecot serves IMAP in the clear, and this
#      module says so outright: `ssl = no` plus the version's spelling of
#      "allow cleartext auth". Neither is inherited — nixpkgs used to emit
#      both whenever sslServerCert was null, and no longer does. Leaving
#      them out gets you a port that accepts connections and fails every
#      login with a message that never mentions TLS. Set `tls` on any
#      network you do not fully control.
#   2. The password file is assembled at runtime under /run, not built
#      into the store. Hashes are secrets; the store is world-readable.
#   3. Dovecot's LMTP and SASL sockets live under Postfix's queue
#      directory, which only exists after postfix-setup has run — hence
#      the explicit ordering below. Without it Dovecot starts first on a
#      fresh host and dies on a missing directory.
#   4. The NixOS unit is `dovecot.service`, not `dovecot2.service`, even
#      though the options live at `services.dovecot2`.
#   5. Two Dovecot config dialects are emitted, and which one a host gets
#      is not this module's choice — see `dovecotPre24` below.
{ config, options, lib, pkgs, ... }:
let
  inherit (lib) mkEnableOption mkOption mkIf types;
  cfg = config.infra.mail.internal;

  vmailUser = "vmail";

  # Whether the consumer's nixpkgs has the RFC42 `settings` rewrite of the
  # dovecot2 module. Everything below is written against it; nixpkgs before
  # that rewrite configured Dovecot through `extraConfig` and a handful of
  # bespoke options instead, and has no `settings` at all.
  #
  # This has to be a RUNTIME attribute test, not a version comparison, and
  # the definitions it guards have to be omitted with `optionalAttrs` rather
  # than `mkIf`. `mkIf false { services.dovecot2.settings = …; }` still
  # registers the path, so the module system still reports "option does not
  # exist" — on EVERY host of a consumer that never enables this module. That
  # is exactly what happened: one fleet pinned a Feb-2026 nixpkgs (dovecot
  # 2.3.21.1, pre-rewrite) and every `nix eval` of every host died here, on a
  # module none of them use. A library module must not be able to do that.
  #
  # The module still fails loudly when it is actually ENABLED on such a
  # nixpkgs — see the assertion in the config block. Silent no-op would be
  # worse: a mail host that accepts nothing looks identical to a working one
  # until someone sends mail.
  hasDovecotSettings = options.services.dovecot2 ? settings;

  # The group Dovecot's own processes run as, spelled differently either
  # side of the rewrite. Only reachable when hasDovecotSettings holds, but
  # kept total so eval reaches the assertion instead of crashing before it.
  dovecotInternalGroup =
    if hasDovecotSettings
    then config.services.dovecot2.settings.default_internal_group
    else config.services.dovecot2.group;

  # Which Dovecot this host will actually run. nixpkgs picks the package
  # from `system.stateVersion` — dovecot_2_3 below 26.05, 2.4 at or above
  # — so the consumer's stateVersion, not fleetkit, decides which config
  # dialect is legal. Both have to work: every fleetkit consumer today
  # resolves to 2.3, and the first one to raise stateVersion gets 2.4
  # without touching this file.
  #
  # The settings themselves are not compatible in either direction. 2.4
  # dropped `mail_location`, `disable_plaintext_auth` and the `args =`
  # form of passdb/userdb; 2.3 rejects `mail_path`, `mail_driver`,
  # `auth_allow_cleartext` and the two mandatory version keys as unknown
  # settings — a fatal parse error, not a warning. The mail-internal
  # check runs each version's own `doveconf` over the rendered file for
  # exactly this reason.
  dovecotPre24 = lib.versionOlder config.services.dovecot2.package.version "2.4";

  # Where one mailbox's Maildir lives. 2.4 retired the short expansion
  # variables in favour of a template language, so the same path has two
  # spellings.
  maildirPath =
    if dovecotPre24
    then "${cfg.stateDir}/%d/%n"
    else "${cfg.stateDir}/%{user | domain}/%{user | username}";

  # Assembled by the oneshot below from the per-mailbox hash files. Under
  # /run because it is reconstructed every boot and must never be in the
  # store.
  runtimeDir = "/run/mail-internal";
  passwdFile = "${runtimeDir}/passwd";

  mailboxNames = builtins.attrNames cfg.mailboxes;

  # Postfix needs a list of valid recipients so an unknown local address
  # is refused during the SMTP conversation instead of accepted and
  # bounced later — a bounce nobody reads is indistinguishable from
  # delivery. Only the *keys* matter: virtual_transport sends every
  # accepted message to Dovecot over LMTP, so Postfix never looks at the
  # mailbox path on the right. `texthash:` reads this file as-is; a
  # `hash:` map would need a postmap run against a file in an immutable
  # store.
  mailboxMap = pkgs.writeText "virtual-mailbox-map" (
    lib.concatMapStrings (n: "${n} unused-lmtp-delivers-this\n") mailboxNames
  );

  # Postfix's queue directory, and the sockets Dovecot opens inside it.
  # virtual_transport names the LMTP one relatively because Postfix
  # resolves a relative unix socket path against queue_directory.
  queueDir = "/var/lib/postfix/queue";

  # A Dovecot `service` block whose only job is to put a socket where
  # Postfix can reach it. Both sockets live in Postfix's queue directory
  # and are owned by Postfix, because Postfix is the only thing that ever
  # connects to them.
  postfixSocketService = serviceName: socket: mode: {
    _section.name = serviceName;
    "unix_listener ${queueDir}/private/${socket}" = {
      inherit mode;
      user = config.services.postfix.user;
      group = config.services.postfix.group;
    };
  };

  # Settings that are spelled the same in 2.3 and 2.4. `service` is a
  # list of sections in both: the renderer takes the section name from
  # `_section.name`, so one definition covers each.
  dovecotCommonSettings = {
    mail_uid = vmailUser;
    mail_gid = vmailUser;

    service =
      [ (postfixSocketService "lmtp" "dovecot-lmtp" "0600") ]
      ++ lib.optional cfg.submission.enable
        (postfixSocketService "auth" "auth" "0660")
      ++ lib.optional cfg.imap.enable {
        _section.name = "imap-login";
        "inet_listener imap".port = cfg.imap.port;
      };
  } // lib.optionalAttrs (cfg.listenAddress != null) {
    listen = cfg.listenAddress;
  };

  # Dovecot 2.3. `args` is a single opaque string the driver parses
  # itself, which is why the static userdb's fields are jammed together
  # here and broken out in 2.4.
  dovecot23Settings = {
    protocols = [ "lmtp" ] ++ lib.optional cfg.imap.enable "imap";
    mail_location = "maildir:${maildirPath}";

    "passdb passwd-file" = {
      driver = "passwd-file";
      args = "scheme=CRYPT username_format=%u ${passwdFile}";
    };

    # Static userdb: one uid owns every Maildir, so there is no
    # per-mailbox system account to manage or to leak a shell.
    "userdb static" = {
      driver = "static";
      args = "uid=${vmailUser} gid=${vmailUser} home=${maildirPath}";
    };
  } // (if cfg.tls == null then {
    ssl = "no";
    disable_plaintext_auth = false;
  } else {
    ssl = "yes";
    # The leading `<` is 2.3's "read this setting from that file"; drop
    # it and Dovecot treats the path as a PEM blob.
    ssl_cert = "<${cfg.tls.certFile}";
    ssl_key = "<${cfg.tls.keyFile}";
  });

  # Dovecot 2.4. The two version keys are mandatory — the nixpkgs module
  # asserts on their absence — and they pin the dialect Dovecot parses,
  # so they track the package rather than being hardcoded.
  dovecot24Settings = {
    dovecot_config_version = config.services.dovecot2.package.version;
    dovecot_storage_version = config.services.dovecot2.package.version;

    protocols = { lmtp = true; imap = cfg.imap.enable; };
    mail_driver = "maildir";
    mail_path = maildirPath;

    "passdb passwd-file" = {
      passwd_file_path = passwdFile;
      default_password_scheme = "CRYPT";
    };

    "userdb static".fields = {
      uid = vmailUser;
      gid = vmailUser;
      home = maildirPath;
    };
  } // (if cfg.tls == null then {
    ssl = "no";
    auth_allow_cleartext = true;
  } else {
    ssl = "yes";
    ssl_server_cert_file = cfg.tls.certFile;
    ssl_server_key_file = cfg.tls.keyFile;
  });

  passwdInit = pkgs.writeShellScript "mail-internal-passwd" ''
    set -euo pipefail
    umask 0077

    install -d -m 0700 -o root -g root ${runtimeDir}
    tmp="${runtimeDir}/.passwd.$$"
    : > "$tmp"

    ${lib.concatMapStrings (n: ''
      if [ ! -r ${lib.escapeShellArg cfg.mailboxes.${n}.passwordFile} ]; then
        echo "mail-internal: cannot read the password file for ${n}" >&2
        exit 1
      fi
      # printf '%s' rather than echo: a crypt hash is full of $ and / and
      # may legitimately end in characters echo would try to interpret.
      printf '%s:%s\n' ${lib.escapeShellArg n} \
        "$(cat ${lib.escapeShellArg cfg.mailboxes.${n}.passwordFile})" >> "$tmp"
    '') mailboxNames}

    # Dovecot's auth process runs as root (the nixpkgs module pins it
    # that way), so root:root 0400 would do. The group read is for
    # `doveadm` run by an operator in the dovecot group.
    chown root:${dovecotInternalGroup} "$tmp"
    chmod 0440 "$tmp"
    mv -f "$tmp" ${passwdFile}
  '';
in
{
  options.infra.mail.internal = {
    enable = mkEnableOption "internal-only mail (Postfix transport + Dovecot mailboxes) for one domain, with no route off the fleet";

    domain = mkOption {
      type = types.str;
      example = "mail.example.com";
      description = ''
        The one domain this host is authoritative for. Mail to any other
        domain is rejected rather than relayed. Mailbox addresses must
        be in it, so changing it renames every account.
      '';
    };

    hostname = mkOption {
      type = types.nullOr types.str;
      default = null;
      example = "mx.example.com";
      description = ''
        Value for Postfix myhostname, used in SMTP greetings and
        Received headers. Null lets Postfix derive it from the system
        hostname.
      '';
    };

    listenAddress = mkOption {
      type = types.nullOr types.str;
      default = null;
      example = "192.0.2.10";
      description = ''
        Single address Postfix and Dovecot bind. Null binds every
        interface, which is the usual shape for a host whose only job is
        mail; set it when the host has an interface that should not
        carry mail.
      '';
    };

    trustedNetworks = mkOption {
      type = types.listOf types.str;
      default = [ "127.0.0.0/8" "::1" ];
      example = [ "127.0.0.0/8" "::1" "192.0.2.0/24" ];
      description = ''
        Networks allowed to submit mail, as Postfix mynetworks. Clients
        outside this list are refused at connection time. This governs
        who may send; it never grants a route off the domain, which no
        client can obtain.
      '';
    };

    mailboxes = mkOption {
      default = { };
      example = lib.literalExpression ''
        {
          "alerts@mail.example.com".passwordFile = "/run/secrets/mail-alerts";
        }
      '';
      description = ''
        Virtual mailboxes, keyed by full address. Each needs a file
        holding a Dovecot password hash — generate one with
        `doveadm pw -s SHA512-CRYPT`. Addresses not listed here are
        rejected during the SMTP conversation.
      '';
      type = types.attrsOf (types.submodule {
        options.passwordFile = mkOption {
          # str, not path: a Nix path literal here would copy the hash
          # into the world-readable store. This wants a runtime path,
          # typically config.sops.secrets.<name>.path.
          type = types.str;
          description = ''
            Path, on the running host, to a file holding this mailbox's
            password hash. Read at activation; the contents never reach
            the Nix store.
          '';
        };
      });
    };

    imap = {
      enable = mkOption {
        type = types.bool;
        default = true;
        description = ''
          Serve IMAP. Turn it off for a host that only accepts and
          stores mail for something else to collect out of band.
        '';
      };
      port = mkOption {
        type = types.port;
        default = 143;
        description = "Port Dovecot serves IMAP on.";
      };
    };

    submission = {
      enable = mkOption {
        type = types.bool;
        default = false;
        description = ''
          Offer the submission port in addition to :25. Only useful when
          clients authenticate rather than being trusted by address;
          within one fleet, trustedNetworks on :25 is usually enough.
        '';
      };
      port = mkOption {
        type = types.port;
        default = 587;
        description = "Port Postfix offers submission on.";
      };
    };

    tls = mkOption {
      type = types.nullOr (types.submodule {
        options = {
          certFile = mkOption {
            # str for the same reason as passwordFile: keep the key off
            # the store even when someone points this at a local file.
            type = types.str;
            description = "Path, on the running host, to the certificate Dovecot presents.";
          };
          keyFile = mkOption {
            type = types.str;
            description = "Path, on the running host, to the matching private key.";
          };
        };
      });
      default = null;
      example = lib.literalExpression ''
        {
          certFile = "/var/lib/acme/mail.example.com/fullchain.pem";
          keyFile = "/var/lib/acme/mail.example.com/key.pem";
        }
      '';
      description = ''
        Certificate for IMAP. Null disables TLS and, necessarily,
        re-enables plaintext authentication — acceptable only on a
        network where nothing untrusted can reach the IMAP port.
      '';
    };

    stateDir = mkOption {
      type = types.str;
      default = "/var/lib/mail-internal";
      description = "Directory holding the Maildirs, one subtree per domain and mailbox.";
    };
  };

  config = mkIf cfg.enable (lib.mkMerge [
   {
    assertions = [
      {
        assertion = hasDovecotSettings;
        message = "infra.mail.internal: this module configures Dovecot through `services.dovecot2.settings`, which your nixpkgs does not have — it predates the RFC42 rewrite of that module (dovecot 2.3 era). Move nixpkgs forward, or leave infra.mail.internal disabled.";
      }
      {
        assertion = cfg.mailboxes != { };
        message = "infra.mail.internal: no mailboxes declared — every recipient would be rejected, which makes the host a listener that stores nothing.";
      }
      {
        assertion = lib.all (n: lib.hasSuffix "@${cfg.domain}" n) mailboxNames;
        message = "infra.mail.internal: every mailbox must be an address in `domain` (${cfg.domain}) — an address outside it can never be delivered to, because this host relays nowhere.";
      }
      {
        assertion = cfg.submission.enable -> cfg.tls != null;
        message = "infra.mail.internal: submission.enable requires tls — the submission port asks for credentials, and offering it without a certificate invites clients to send them in the clear.";
      }
    ];

    # The vmail user and group come from Dovecot's own createMailUser
    # (default true) once settings.mail_uid/mail_gid are set below;
    # declaring them again here would collide on `description`. Only the
    # home is ours to add.
    users.users.${vmailUser}.home = cfg.stateDir;

    systemd.tmpfiles.rules = [
      "d ${cfg.stateDir} 0700 ${vmailUser} ${vmailUser} -"
    ];

    services.postfix = {
      enable = true;
      settings.main = {
        myhostname = cfg.hostname;
        mydomain = cfg.domain;
        myorigin = "$mydomain";
        inet_interfaces = if cfg.listenAddress == null then "all" else cfg.listenAddress;

        # Empty on purpose. Local delivery via /etc/passwd would give
        # every system account on this host a mailbox; the only accounts
        # that exist here are the virtual ones declared above.
        mydestination = [ ];

        virtual_mailbox_domains = [ cfg.domain ];
        virtual_mailbox_maps = "texthash:${mailboxMap}";
        virtual_transport = "lmtp:unix:private/dovecot-lmtp";

        mynetworks = cfg.trustedNetworks;

        # The three settings that make this internal-only.
        #
        # relayhost and relay_domains empty means there is nowhere to
        # forward to even if a restriction were misread. Both already
        # default to [ ] in the nixpkgs module; they are restated here
        # because this module's whole contract rests on them, and the
        # check in nix/checks.nix reads them back. Then
        # smtpd_relay_restrictions is *just* reject_unauth_destination —
        # note what is absent: Postfix's built-in value leads with
        # permit_mynetworks, which would let every trusted client relay
        # to the internet. Dropping it is the whole point, so do not
        # "fix" this by restoring the default.
        relayhost = [ ];
        relay_domains = [ ];
        smtpd_relay_restrictions = [ "reject_unauth_destination" ];

        # Who may talk to us at all, and which local recipients exist.
        smtpd_client_restrictions = [ "permit_mynetworks" "reject" ];
        smtpd_recipient_restrictions = [ "reject_unlisted_recipient" "permit" ];

        # Nothing here ever speaks to a public MX, so certificate
        # verification on outbound has nothing to verify.
        smtp_tls_security_level = "none";
      };

      enableSubmission = cfg.submission.enable;
      # A definition replaces the option's default wholesale, so this
      # restates everything the nixpkgs default carried that still
      # applies, minus milter_macro_daemon_name (no milters here).
      submissionOptions = lib.mkIf cfg.submission.enable {
        smtpd_tls_security_level = "encrypt";
        smtpd_sasl_auth_enable = "yes";
        smtpd_sasl_type = "dovecot";
        smtpd_sasl_path = "private/auth";
        smtpd_client_restrictions = "permit_sasl_authenticated,reject";
        # Repeated here because master.cf options override main.cf per
        # service: without it the submission port would fall back to
        # Postfix's permissive built-in relay restrictions.
        smtpd_relay_restrictions = "reject_unauth_destination";
      };
    };

    # Dovecot opens its LMTP (and SASL) socket inside Postfix's queue
    # directory. postfix-setup is what creates that tree, so on a host
    # booting for the first time Dovecot loses the race without this.
    systemd.services.dovecot = {
      after = [ "postfix-setup.service" ];
      requires = [ "postfix-setup.service" ];
    };

    systemd.services.mail-internal-passwd = {
      description = "Assemble the Dovecot password file from the mailbox secrets";
      wantedBy = [ "multi-user.target" ];
      before = [ "dovecot.service" ];
      requiredBy = [ "dovecot.service" ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        ExecStart = passwdInit;
      };
    };

    infra.services.mail-internal = {
      port = 25;
      extraPorts =
        lib.optional cfg.imap.enable {
          port = cfg.imap.port;
          name = "imap";
          protocol = "imap";
        }
        ++ lib.optional cfg.submission.enable {
          port = cfg.submission.port;
          name = "submission";
          protocol = "smtp";
        };
      description = "Internal-only mail for one domain — Postfix transport, Dovecot mailboxes";
      category = "mail";
      tags = [ "mail" "smtp" "imap" "internal" ];
      # SMTP and IMAP are not HTTP; there is nothing for Caddy to proxy.
      caddy.enable = false;
    };
   }

   # `optionalAttrs`, not `mkIf` — see hasDovecotSettings above. When the
   # consumer's nixpkgs predates the dovecot2 rewrite this attrset is empty,
   # so `services.dovecot2.settings` is never a definition path and never an
   # unknown-option error. The assertion above is what surfaces it.
   (lib.optionalAttrs hasDovecotSettings {
     services.dovecot2 = {
       enable = true;

       # No PAM: these accounts are not system users, and leaving the PAM
       # passdb in place would let a local shell account log in as mail.
       # This is already the option's default; it is restated because the
       # mail-internal check asserts on it, and a default that quietly
       # flips is exactly the regression that check exists to catch.
       enablePAM = false;

       settings = dovecotCommonSettings
         // (if dovecotPre24 then dovecot23Settings else dovecot24Settings);
     };
   })
  ]);
}
