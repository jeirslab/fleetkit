# Evaluation tests of the schema: the fixture registry evaluates and reports
# what it should; each broken variant fails with the message that names it.
# -> { passed = [ names ]; failed = [ "name: why" ]; }
{ lib, self }:
let
  fixture = ./fixtures/registry.nix;
  evalWith =
    extra:
    self.lib.secrets.evalRegistry {
      registry = fixture;
      extraModules = extra;
    };
  base = evalWith [ ];
  report = base.config.fleet.report;

  # A case that must evaluate with every assertion holding and `test` true.
  good = name: test: {
    inherit name;
    ok = base.failures == [ ] && test;
    why = "failures: ${builtins.toJSON base.failures}";
  };
  # A case that must fail with a message containing `want`.
  bad =
    name: extra: want:
    let
      ev = evalWith extra;
      hit = lib.any (m: lib.hasInfix want m) ev.failures;
    in
    {
      inherit name;
      ok = hit;
      why = "expected a failure containing \"${want}\", got ${builtins.toJSON ev.failures}";
    };
  # A case whose evaluation must throw (a type error).
  throws =
    name: extra: path:
    let
      ev = evalWith extra;
    in
    {
      inherit name;
      ok = !(builtins.tryEval (builtins.deepSeq (lib.attrByPath path null ev.config) true)).success;
      why = "expected ${lib.concatStringsSep "." path} not to evaluate";
    };

  q = c: "age1" + lib.concatStrings (builtins.genList (_: c) 58);
  infisical = report.secrets.placements.infisical;
  bitwarden = report.secrets.placements.bitwarden;

  # nixfisical's lib.mkInfisical and nixwarden's lib.mkBitwarden return these
  # shapes (nixfisical b2c2bc2, nixwarden c14b7ac); they must be assignable as is.
  mkInfisical =
    {
      project,
      folder ? "/",
      environment ? "prod",
      name ? null,
      groups ? [ ],
      source ? "sops",
    }:
    {
      inherit
        project
        folder
        environment
        name
        groups
        source
        ;
    };
  mkBitwardenResult = {
    org = "example";
    collection = "ops";
    type = "note";
    uris = [ ];
    favorite = false;
    reprompt = false;
    name = null;
    field = "notes";
    username = null;
    totp = null;
    notes = null;
    fields = { };
  };

  cases = [
    (good "fixture evaluates" true)
    (good "ref derived" (
      base.config.fleet.estates.apps.secrets.files.web.ref."admin/password"
      == "sops:apps/web#admin/password"
    ))
    (good "rules and anchors (fleetkit shape)" (
      report.sops.apps.rules == [
        {
          path = "apps/db.yaml";
          anchors = [
            "admin"
            "host_db"
            "host_web"
          ];
        }
        {
          path = "apps/web.yaml";
          anchors = [
            "admin"
            "host_web"
          ];
        }
      ]
      &&
        report.sops.apps.anchors.host_db.ssh
        == "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPlaceholderPlaceholderPlaceholder00"
      && report.sops.ops.anchors.host_node1.id == "lan/node1"
    ))
    (good "sopsConfig order and layout" (
      map (e: e.name) report.secrets.sopsConfig.estates == [
        "apps"
        "ops"
        "legacy"
      ]
      &&
        (lib.last report.secrets.sopsConfig.estates).rules == [
          {
            pathRegex = "secrets/vpn\\.yaml$";
            anchors = [ "master" ];
          }
          {
            pathRegex = "^secrets/app\\.yaml$";
            anchors = [
              "master"
              "app_host"
            ];
          }
        ]
    ))
    (good "infisical manifest (nixfisical entry shape)" (
      map (e: "${e.sopsFile}#${e.sopsKey}@${e.project}/${e.environment}${e.folder}:${e.name}") infisical
      == [
        "apps/db.yaml#password@apps/prod/db:password"
        "apps/web.yaml#session_key@apps/prod/web:session_key"
      ]
      &&
        (lib.head infisical).hosts == [
          "apps/db"
          "apps/web"
        ]
    ))
    (good "bitwarden manifest (nixwarden entry shape)" (
      let
        e = lib.head bitwarden;
      in
      lib.length bitwarden == 1
      && e.field == "password"
      && e.name == "Web admin"
      &&
        e.username == {
          sopsKey = "admin/user";
          sopsFile = null;
        }
    ))
    (good "sopsRef (lib/secrets/sops-ref.nix)" (
      self.lib.secrets.sopsRef {
        fleet = base.config.fleet;
        estate = "ops";
        where = "test";
      } "sops:apps/web#admin/password" == {
        name = "apps_web";
        path = "apps/web.yaml";
        key = "admin.password";
        extract = [
          "admin"
          "password"
        ];
        expr = "\${data.sops_file.apps_web.data[\"admin.password\"]}";
      }
    ))
    (good "no host drift standalone" (report.secrets.hostDrift == [ ]))
    {
      name = "mkInfisical and mkBitwarden results are assignable";
      ok =
        (evalWith [
          {
            fleet.estates.ops.secrets.files.node.placements."wifi/password" = {
              infisical = mkInfisical {
                project = "ops";
                folder = "/wifi";
              };
              bitwarden = mkBitwardenResult;
            };
          }
        ]).failures == [ ];
      why = "assigning the library results failed";
    }

    (bad "placement on an undeclared key" [
      { fleet.estates.apps.secrets.files.db.placements.nope.infisical.project = "apps"; }
    ] "\"nope\" is not a declared key of the file")
    (bad "two keys at one Infisical destination" [
      {
        fleet.estates.ops.secrets.files.node.placements."wifi/password".infisical = {
          project = "apps";
          folder = "/db";
          name = "password";
        };
      }
    ] "same Infisical destination")
    (bad "two keys in one Bitwarden item" [
      {
        fleet.estates.apps.secrets.files.db.placements.password.bitwarden = {
          org = "example";
          collection = "apps";
          name = "Web admin";
        };
      }
    ] "same Bitwarden item")
    (bad "relative Infisical folder" [
      {
        fleet.estates.apps.secrets.files.web.placements."admin/user".infisical = {
          project = "apps";
          folder = "web";
        };
      }
    ] "must be absolute")
    (bad "Infisical environment slug" [
      {
        fleet.estates.apps.secrets.files.web.placements."admin/user".infisical = {
          project = "apps";
          environment = "Prod";
        };
      }
    ] "must match [a-z0-9-]+")
    (bad "Bitwarden reference to an undeclared key" [
      {
        fleet.estates.apps.secrets.files.web.placements.session_key.bitwarden = {
          org = "example";
          collection = "apps";
          notes = {
            sopsKey = "nope";
          };
          type = "note";
        };
      }
    ] "\"nope\" is not a declared key of fleet.estates.apps.secrets.files.web")
    (bad "Bitwarden login-only slot on a note" [
      {
        fleet.estates.apps.secrets.files.web.placements.session_key.bitwarden = {
          org = "example";
          collection = "apps";
          type = "note";
          username = "u";
        };
      }
    ] "only apply to login items")
    (bad "Bitwarden field not a slot of the type" [
      {
        fleet.estates.apps.secrets.files.web.placements.session_key.bitwarden = {
          org = "example";
          collection = "apps";
          type = "note";
          field = "password";
        };
      }
    ] "is not a slot of a note item")
    (bad "Bitwarden reserved marker field" [
      {
        fleet.estates.apps.secrets.files.web.placements.session_key.bitwarden = {
          org = "example";
          collection = "apps";
          fields.nixwarden = "x";
        };
      }
    ] "reserved for nixwarden")
    (bad "verbatim rule with an unknown anchor" [
      {
        fleet.estates.legacy.secrets.layout.rules = [
          {
            pathRegex = "x$";
            anchors = [ "nobody" ];
          }
        ];
      }
    ] "anchor \"nobody\" is not one of the estate's")
    (bad "anchor names clash across estates" [
      { fleet.estates.legacy.secrets.layout.prefix = lib.mkForce ""; }
      {
        fleet.estates.legacy.secrets.layout.keys = [
          {
            name = "admin";
            age = q "c";
          }
        ];
      }
    ] "anchor names clash")
    (bad "sopsConfig names an unknown estate" [
      { fleet.sopsConfig.estates = lib.mkForce [ "nowhere" ]; }
    ] "\"nowhere\" is not a declared estate")
    (bad "secretHosts id without an estate" [
      { fleet.secretHosts.lonely.age = q "c"; }
    ] "a host id is <estate or site>/<name>")
    # fleetkit's own assertions, unchanged.
    (bad "reader is not a host" [
      { fleet.estates.apps.secrets.files.web.readers = [ "apps/ghost" ]; }
    ] "host reference \"apps/ghost\" does not exist")
    (bad "readers share an anchor name" [
      {
        fleet.secretHosts."ops/web".age = q "c";
        fleet.estates.apps.secrets.files.web.readers = [ "ops/web" ];
      }
    ] "readers share a sops anchor name")
    (bad "undeclared operator" [
      { fleet.estates.apps.secrets.files.web.operators = [ "nobody" ]; }
    ] "\"nobody\" is not declared in fleet.estates.apps.secrets.operators")
    (bad "file without keys" [
      {
        fleet.estates.apps.secrets.files.empty = {
          path = "apps/empty.yaml";
          keys = [ ];
        };
      }
    ] "must declare at least one key")
    (throws "malformed age recipient"
      [
        { fleet.estates.apps.secrets.operators.bad.age = "age1short"; }
      ]
      [
        "fleet"
        "estates"
        "apps"
        "secrets"
        "operators"
        "bad"
        "age"
      ]
    )
    (throws "Infisical source outside sops|infisical"
      [
        {
          fleet.estates.apps.secrets.files.web.placements."admin/user".infisical = {
            project = "apps";
            source = "Infisical";
          };
        }
      ]
      [
        "fleet"
        "estates"
        "apps"
        "secrets"
        "files"
        "web"
        "placements"
        "admin/user"
        "infisical"
        "source"
      ]
    )
  ];
in
{
  passed = map (c: c.name) (lib.filter (c: c.ok) cases);
  failed = map (c: "${c.name}: ${c.why}") (lib.filter (c: !c.ok) cases);
}
