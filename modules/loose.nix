# Estate-level loose blocks: git, cache, build, observability, substrate,
# gpu, llm, storage, colmena. Each is inventoried in
# fleet.report.looseBlocks and docs/schema-todo.md.
#
# Each block is a freeform submodule (lazyAttrsOf anything, no declared
# options, so values keep exactly the shape config gives them and a value
# produced by one estate re-merges into another). The key set of every block
# and of its nested elements is closed by the keysOk assertions below (an
# unknown key fails with the allowed list), and every reference leaf and
# tagged union inside is validated too. Leaves are read with `or` defaults
# so an absent leaf is simply not checked.
{ lib, config, ... }:
let
  inherit (lib) types mkOption;
  h = import ./lib.nix { inherit lib; };
  ids = config.fleet.report.ids;

  looseBlock =
    what:
    mkOption {
      type = types.nullOr (
        types.submodule {
          freeformType = types.lazyAttrsOf types.anything;
        }
      );
      default = null;
      description = "Loose block (${what}); see docs/schema-todo.md.";
    };

  hostIds = ids.guest ++ ids.node;

  ref = where: kind: idList: h.refAssertion { inherit where kind; ids = idList; };
  refs = where: kind: idList: map (ref where kind idList);
  guestRef = where: ref where "guest" ids.guest;
  networkRef = where: ref where "network" ids.network;
  hostRef = where: ref where "guest-or-node" hostIds;

  # Optional leaf: checked only when present.
  # `f` returns a list of assertions.
  opt = attrs: name: f: lib.optionals (attrs ? ${name}) (f attrs.${name});
  optList = opt;

  # Closed key sets: a loose block still rejects misspelled keys, so a
  # reference cannot hide under a typo. Non-attrset values are left to the
  # per-leaf checks.
  keysOk =
    where: allowed: attrs:
    lib.optionals (builtins.isAttrs attrs) (
      map (k: {
        assertion = builtins.elem k allowed;
        message = "${lib.removeSuffix "." where}: unknown key \"${k}\" (allowed: ${lib.concatStringsSep ", " allowed})";
      }) (builtins.attrNames attrs)
    );
  keysOkEach =
    where: allowed: list:
    lib.concatLists (lib.imap0 (i: x: keysOk "${where}[${toString i}]" allowed x) (
      if builtins.isList list then list else [ ]
    ));

  # sops reference: a string declared by one of `estate`'s secrets files.
  # Returns a list of assertions.
  sopsRef =
    estate: where: value:
    if builtins.isString value then
      h.secretRefAssertions { inherit where estate; ids = ids.secretRef; } value
    else
      [
        {
          assertion = false;
          message = "${where}: expected a sops reference \"sops:<estate>/<file>#<key>\", got <non-string>";
        }
      ];

  gitChecks =
    estate: g:
    let
      w = p: "fleet.estates.${estate}.git.${p}";
      prefix = "${estate}/";
      repoRefs = where: refs where "repo" ids.repo;
      principalRefs = where: refs where "principal" ids.principal;
      members = g.members or { };
      teams = g.teams or { };
      rulesets = g.rulesets or { };
      secrets = g.actions.secrets or { };
      auth = g.auth or null;
      authKind = auth.kind or null;
      authNeeds =
        if authKind == "token" then
          [ "tokenRef" ]
        else if authKind == "app" then
          [
            "appIdRef"
            "installationIdRef"
            "pemRef"
          ]
        else
          [ ];
      # github_organization_settings arguments beyond the first set: profile
      # text and member privileges, each checked for its type when present.
      orgText = [
        "blog"
        "company"
        "description"
        "email"
        "location"
        "name"
        "twitterUsername"
      ];
      orgBool = [
        "advancedSecurityEnabledForNewRepositories"
        "dependabotSecurityUpdatesEnabledForNewRepositories"
        "membersCanCreateInternalRepositories"
        "membersCanCreatePages"
        "membersCanCreatePrivatePages"
        "membersCanCreatePublicPages"
        "membersCanForkPrivateRepositories"
      ];
      # The named leaves of a block, when present, satisfy `ok`.
      typed =
        where: what: ok: names: attrs:
        lib.optionals (builtins.isAttrs attrs) (
          map (n: {
            assertion = ok attrs.${n};
            message = "${where}.${n}: expected ${what}";
          }) (lib.filter (n: attrs ? ${n}) names)
        );
      plan = g.plan or "free";
      plans = [
        "free"
        "pro"
        "team"
        "enterprise"
      ];
    in
    [
      {
        assertion = builtins.isString plan && builtins.elem plan plans;
        message = "${w "plan"}: \"${toString plan}\" is not one of ${lib.concatStringsSep ", " plans}";
      }
      {
        assertion = !((g.kind or null) == "org" && plan == "pro");
        message = "${w "plan"}: \"pro\" is a personal account's plan; an organisation (kind = \"org\") is on free, team or enterprise";
      }
    ]
    ++ keysOk (w "") [
      "actions"
      "auth"
      "backend"
      "hostAccess"
      "infrastructureRepo"
      "kind"
      "members"
      "org"
      "organization"
      "plan"
      "platform"
      "rulesets"
      "stack"
      "teams"
    ] g
    ++ keysOk (w "auth") [ "appIdRef" "installationIdRef" "kind" "pemRef" "tokenRef" ] (g.auth or { })
    ++ keysOk (w "hostAccess") [ "method" "readOnly" ] (g.hostAccess or { })
    ++ keysOk (w "members") [ "admin" "member" "outside" ] members
    ++ keysOk (w "organization") (
      [
        "billingEmail"
        "defaultRepositoryPermission"
        "dependabotAlertsEnabledForNewRepositories"
        "dependencyGraphEnabledForNewRepositories"
        "hasOrganizationProjects"
        "hasRepositoryProjects"
        "membersCanCreatePrivateRepositories"
        "membersCanCreatePublicRepositories"
        "membersCanCreateRepositories"
        "secretScanningEnabledForNewRepositories"
        "secretScanningPushProtectionEnabledForNewRepositories"
        "webCommitSignoffRequired"
      ]
      ++ orgText
      ++ orgBool
    ) (g.organization or { })
    ++ typed (w "organization") "a string" builtins.isString orgText (g.organization or { })
    ++ typed (w "organization") "true or false" builtins.isBool orgBool (g.organization or { })
    ++ lib.concatLists (
      lib.mapAttrsToList (
        t: typed (w "teams.${t}") "a string" builtins.isString [ "description" ]
      ) teams
    )
    ++ keysOk (w "actions") [
      "allowedActions"
      "enabledRepositories"
      "runnerGroups"
      "secrets"
      "shaPinningRequired"
      "variables"
      "workflowPermissions"
    ] (g.actions or { })
    ++ lib.concatLists (
      lib.mapAttrsToList (n: sv: keysOk (w "actions.secrets.${n}") [ "sourceRef" "repos" ] sv) secrets
    )
    ++ lib.concatLists (
      lib.mapAttrsToList (
        t: team:
        keysOk (w "teams.${t}") [ "description" "members" "privacy" "repos" ] team
        ++ keysOkEach (w "teams.${t}.repos") [ "repo" "permission" ] (team.repos or [ ])
      ) teams
    )
    ++ lib.concatLists (
      lib.mapAttrsToList (
        r: rs:
        keysOk (w "rulesets.${r}") [
          "enforcement"
          "refs"
          "repositories"
          "requiresPlan"
          "rules"
          "target"
        ] rs
      ) rulesets
    )
    ++ opt g "infrastructureRepo" (
      v:
      [
        (ref (w "infrastructureRepo") "repo" ids.repo v)
        {
          assertion = lib.hasPrefix prefix v;
          message = "${w "infrastructureRepo"}: repo \"${v}\" must belong to estate \"${estate}\" (id prefix \"${prefix}\")";
        }
      ]
    )
    ++ opt g "backend" (v: [ (ref (w "backend") "backend" ids.backend v) ])
    ++ lib.concatMap (
      role: principalRefs (w "members.${role}") (members.${role} or [ ])
    ) [ "admin" "member" "outside" ]
    ++ lib.concatLists (
      lib.mapAttrsToList (
        t: team:
        principalRefs (w "teams.${t}.members") (team.members or [ ])
        ++ lib.concatMap (
          r:
          [
            (ref (w "teams.${t}.repos[].repo") "repo" ids.repo r.repo)
            {
              assertion = builtins.elem r.permission [
                "pull"
                "triage"
                "push"
                "maintain"
                "admin"
              ];
              message = "${w "teams.${t}.repos[].permission"}: \"${toString r.permission}\" is not one of pull, triage, push, maintain, admin";
            }
          ]
        ) (team.repos or [ ])
      ) teams
    )
    ++ lib.concatLists (
      lib.mapAttrsToList (r: rs: repoRefs (w "rulesets.${r}.repositories") (rs.repositories or [ ])) rulesets
    )
    ++ lib.concatLists (
      lib.mapAttrsToList (
        n: s:
        sopsRef estate (w "actions.secrets.${n}.sourceRef") (s.sourceRef or null)
        ++ repoRefs (w "actions.secrets.${n}.repos") (s.repos or [ ])
      ) secrets
    )
    ++ lib.optionals (auth != null) (
      [
        {
          assertion = builtins.elem authKind [
            "token"
            "app"
          ];
          message = "${w "auth.kind"}: \"${toString authKind}\" is not one of token, app";
        }
      ]
      ++ lib.concatMap (k: [
        {
          assertion = auth ? ${k};
          message = "${w "auth.${k}"}: required when auth.kind is \"${toString authKind}\" but missing";
        }
      ] ++ opt auth k (sopsRef estate (w "auth.${k}"))) authNeeds
    );

  cacheChecks =
    estate: c:
    let
      w = p: "fleet.estates.${estate}.cache.${p}";
    in
    keysOk (w "") [ "fallback" "substituters" ] c
    ++ keysOkEach (w "substituters") [
      "host"
      "network"
      "port"
      "priority"
      "publicKey"
      "signingKeyRef"
      "url"
    ] (c.substituters or [ ])
    ++ lib.concatLists (
      lib.imap0 (
        i: s:
        let
          at = "substituters[${toString i}]";
        in
        [
          {
            assertion = (s ? host) != (s ? url);
            message = "${w at}: exactly one of host or url must be set";
          }
        ]
        ++ opt s "host" (v: [ (hostRef (w "${at}.host") v) ])
        ++ opt s "network" (v: [ (networkRef (w "${at}.network") v) ])
        ++ opt s "signingKeyRef" (sopsRef estate (w "${at}.signingKeyRef"))
        ++ lib.optional (s ? host) {
          assertion = s ? network;
          message = "${w at}.network: required when host is set";
        }
      ) (c.substituters or [ ])
    );

  buildChecks =
    estate: b:
    let
      w = p: "fleet.estates.${estate}.build.${p}";
    in
    keysOk (w "") [ "hydra" "workers" ] b
    ++ keysOk (w "hydra") [ "guest" ] (b.hydra or { })
    ++ keysOkEach (w "workers") [
      "host"
      "maxJobs"
      "network"
      "sshUser"
      "supportedFeatures"
      "systems"
    ] (b.workers or [ ])
    ++ (lib.optionals (b ? hydra) (opt b.hydra "guest" (v: [ (guestRef (w "hydra.guest") v) ])))
    ++ lib.concatLists (
      lib.imap0 (
        i: wk:
        opt wk "host" (v: [ (hostRef (w "workers[${toString i}].host") v) ])
        ++ opt wk "network" (v: [ (networkRef (w "workers[${toString i}].network") v) ])
      ) (b.workers or [ ])
    );

  observabilityChecks =
    estate: o:
    let
      w = p: "fleet.estates.${estate}.observability.${p}";
    in
    lib.concatLists (
      lib.mapAttrsToList (
        k: t:
        keysOk (w k) [ "host" "network" "path" "port" ] t
        ++ opt t "host" (v: [ (guestRef (w "${k}.host") v) ])
        ++ opt t "network" (v: [ (networkRef (w "${k}.network") v) ])
      ) o
    );

  substrateChecks =
    estate: s:
    let
      w = p: "fleet.estates.${estate}.substrate.${p}";
      roles = [
        "dns"
        "ca"
        "cache"
        "identity"
        "ingress"
        "observability"
      ];
      se = s.secretsEngine or null;
    in
    keysOk (w "") ([ "network" "secretsEngine" ] ++ roles) s
    ++ keysOk (w "secretsEngine") [ "guests" "kind" ] se
    ++ opt s "network" (v: [ (networkRef (w "network") v) ])
    ++ lib.concatMap (r: optList s r (refs (w r) "guest" ids.guest)) roles
    ++ lib.optionals (se != null) (
      opt se "kind" (v: [
        {
          assertion = builtins.elem v [
            "infisical"
            "sops"
          ];
          message = "${w "secretsEngine.kind"}: \"${toString v}\" is not one of infisical, sops";
        }
      ])
      ++ optList se "guests" (refs (w "secretsEngine.guests") "guest" ids.guest)
    );

  gpuChecks =
    estate: g:
    keysOk "fleet.estates.${estate}.gpu" [ "devices" ] g
    ++ optList g "devices" (refs "fleet.estates.${estate}.gpu.devices" "pcie" ids.pcie);

  storageChecks =
    estate: s:
    keysOk "fleet.estates.${estate}.storage" [ "pools" ] s
    ++ optList s "pools" (refs "fleet.estates.${estate}.storage.pools" "storage" ids.storage);

  llmChecks =
    estate: l:
    let
      w = p: "fleet.estates.${estate}.llm.${p}";
    in
    keysOk (w "") [ "gateway" "port" "reach" ] l
    ++ opt l "gateway" (v: [ (guestRef (w "gateway") v) ])
    ++ optList l "reach" (refs (w "reach") "network" ids.network);

  colmenaChecks =
    estate: c:
    let
      w = p: "fleet.estates.${estate}.colmena.${p}";
      boolOrNull = k: opt c k (v: [
        {
          assertion = v == null || builtins.isBool v;
          message = "${w k}: must be a bool or null";
        }
      ]);
    in
    keysOk (w "") [ "targetUser" "buildOnTarget" "excludeAdopted" ] c
    ++ opt c "targetUser" (v: [
      {
        assertion = builtins.isString v;
        message = "${w "targetUser"}: must be a string";
      }
    ])
    ++ boolOrNull "buildOnTarget"
    ++ boolOrNull "excludeAdopted";

  perBlock =
    estate: e: block: f:
    lib.optionals (e.${block} != null) (f estate e.${block});
in
{
  options.fleet.estates = mkOption {
    type = types.attrsOf (
      types.submodule {
        options = {
          git = looseBlock "git platform, org settings, teams, rulesets";
          cache = looseBlock "nix binary cache substituters";
          build = looseBlock "nix build farm";
          observability = looseBlock "metrics/log push targets";
          substrate = looseBlock "substrate role -> guests";
          gpu = looseBlock "pcie devices the estate may use";
          llm = looseBlock "LLM gateway";
          storage = looseBlock "storage pools the estate may use";
          colmena = looseBlock "colmena deployment settings";
        };
      }
    );
  };

  config.assertions = lib.concatLists (
    lib.mapAttrsToList (
      estate: e:
      perBlock estate e "git" gitChecks
      ++ perBlock estate e "cache" cacheChecks
      ++ perBlock estate e "build" buildChecks
      ++ perBlock estate e "observability" observabilityChecks
      ++ perBlock estate e "substrate" substrateChecks
      ++ perBlock estate e "gpu" gpuChecks
      ++ perBlock estate e "storage" storageChecks
      ++ perBlock estate e "llm" llmChecks
      ++ perBlock estate e "colmena" colmenaChecks
    ) config.fleet.estates
  );
}
