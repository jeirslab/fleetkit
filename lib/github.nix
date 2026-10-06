# mkGithubTerraform: render one estate's GitHub repositories and organisation
# as a main.tf.json attrset (builtins.toJSON it). Evaluation only; nothing
# here runs tofu.
#
#   mkGithubTerraform {
#     fleet;     # the checked model (config.fleet)
#     estate;    # estate name
#   } -> { terraform; provider; data; resource; locals; }
#
# Inputs: fleet.repos.<estate>.<key> (typed) and fleet.estates.<estate>.git
# (a loose block). Resource address: <resource type>.<repo key> for a
# repository and what hangs off it; <type>.<team key> for teams;
# <type>.<principal id> for memberships; <type>.<ruleset key> for rulesets.
#
# Credentials are never literals: the provider's app_auth / token and every
# Actions secret value read the estate's secret refs (sops:<estate>/<file>#<key>)
# through data.sops_file, as lib/terraform.nix does (lib/sops-ref.nix).
#
# Repositories are never destroyed by removal: archive_on_destroy = true and
# lifecycle.prevent_destroy = true. A fork is rendered like any repository
# (the provider cannot create a fork relationship); the keys are listed in
# locals.fleet_forks. What the model holds but cannot be rendered is listed in
# locals.fleet_unrendered; rulesets above git.plan in
# locals.fleet_skipped_rulesets.
{
  lib,
  fleet,
  estate,
}:
let
  where = "mkGithubTerraform: fleet.estates.${estate}.git";
  sopsRef = import ./sops-ref.nix { inherit fleet estate where; };

  g =
    fleet.estates.${estate}.git
      or (throw "${where}: the estate has no git block");
  repos = fleet.repos.${estate} or { };
  isOrg = (g.kind or null) == "org";
  org = g.org or (throw "${where}.org: required (the GitHub owner)");

  snake = s: lib.concatMapStrings (c: if lib.toLower c != c then "_${lib.toLower c}" else c) (lib.stringToCharacters s);
  snakeKeys = lib.mapAttrs' (n: v: lib.nameValuePair (snake n) v);
  # Recursive form for pass-through blocks (ruleset rules).
  snakeDeep =
    v:
    if builtins.isAttrs v then
      lib.mapAttrs' (n: x: lib.nameValuePair (snake n) (snakeDeep x)) v
    else if builtins.isList v then
      map snakeDeep v
    else
      v;

  ref = a: "\${${a}}";

  # Repository ids are <estate>/<key>; the key is the address, the GitHub name
  # is the repo's name or its key.
  repoKey = id: lib.removePrefix "${estate}/" id;
  nameOf = k: repos.${k}.name or null;
  ghName = k: if nameOf k != null then nameOf k else k;
  repoRef = id: ref "github_repository.${repoKey id}.name";
  repoId = id: ref "github_repository.${repoKey id}.repo_id";

  # ---- secrets -----------------------------------------------------------
  auth = g.auth or null;
  secrets = g.actions.secrets or { };
  usedRefs =
    lib.optionals (auth != null) (
      if auth.kind == "app" then
        [
          auth.appIdRef
          auth.installationIdRef
          auth.pemRef
        ]
      else
        [ auth.tokenRef ]
    )
    ++ lib.mapAttrsToList (_: s: s.sourceRef) secrets;
  resolved = map sopsRef usedRefs;
  sopsData = lib.listToAttrs (
    map (r: lib.nameValuePair r.name { source_file = r.path; }) resolved
  );

  provider =
    {
      owner = org;
    }
    // lib.optionalAttrs (auth != null) (
      if auth.kind == "app" then
        {
          app_auth = {
            id = (sopsRef auth.appIdRef).expr;
            installation_id = (sopsRef auth.installationIdRef).expr;
            pem_file = (sopsRef auth.pemRef).expr;
          };
        }
      else if auth.kind == "token" then
        {
          token = (sopsRef auth.tokenRef).expr;
        }
      else
        throw "${where}.auth.kind: \"${toString auth.kind}\" is not one of token, app"
    );

  # ---- repositories ------------------------------------------------------
  renderRepo =
    k: r:
    {
      name = ghName k;
      inherit (r) visibility archived;
      # A repository removed from the model is archived, never deleted.
      archive_on_destroy = true;
      lifecycle.prevent_destroy = true;
    }
    // lib.optionalAttrs (r.description != null) { inherit (r) description; }
    // lib.optionalAttrs (r.features != null) {
      has_issues = r.features.issues;
      has_wiki = r.features.wiki;
      has_projects = r.features.projects;
    }
    // lib.optionalAttrs (r.merge != null) {
      allow_merge_commit = r.merge.commit;
      allow_squash_merge = r.merge.squash;
      allow_rebase_merge = r.merge.rebase;
      delete_branch_on_merge = r.merge.deleteBranchOnMerge;
    };

  withDefaultBranch = lib.filterAttrs (_: r: r.defaultBranch != null) repos;
  branchDefaults = lib.mapAttrs (k: r: {
    repository = ref "github_repository.${k}.name";
    branch = r.defaultBranch;
  }) withDefaultBranch;

  envList = lib.concatLists (
    lib.mapAttrsToList (
      k: r: lib.mapAttrsToList (env: e: { inherit k env e; }) r.environments
    ) repos
  );
  envKey = x: "${x.k}_${x.env}";
  environments = lib.listToAttrs (
    map (
      x:
      lib.nameValuePair (envKey x) (
        {
          repository = ref "github_repository.${x.k}.name";
          environment = x.env;
        }
        // lib.optionalAttrs (x.e.branches != [ ]) {
          deployment_branch_policy = {
            protected_branches = false;
            custom_branch_policies = true;
          };
        }
      )
    ) envList
  );
  envPolicies = lib.listToAttrs (
    lib.concatMap (
      x:
      map (
        b:
        lib.nameValuePair "${envKey x}_${builtins.replaceStrings [ "/" "*" ] [ "_" "_" ] b}" {
          repository = ref "github_repository.${x.k}.name";
          environment = ref "github_repository_environment.${envKey x}.environment";
          branch_pattern = b;
        }
      ) x.e.branches
    ) envList
  );

  # The model has no public key for a deploy key, so none is rendered.
  deployKeys = lib.concatLists (
    lib.mapAttrsToList (k: r: map (d: "deploy_key:${k}.${d}") (lib.attrNames r.deployKeys)) repos
  );

  # ---- organisation ------------------------------------------------------
  principals = fleet.operators.principals;
  loginOf =
    p:
    let
      pr = principals.${p} or (throw "${where}: principal \"${p}\" is not declared");
    in
    if pr.github == null then throw "${where}: principal \"${p}\" has no github login" else pr.github;

  members = g.members or { };
  memberRole =
    lib.genAttrs (members.member or [ ]) (_: "member") // lib.genAttrs (members.admin or [ ]) (_: "admin");
  memberships = lib.mapAttrs (p: role: {
    username = loginOf p;
    inherit role;
  }) memberRole;

  teams = g.teams or { };
  teamResources = lib.mapAttrs (
    t: team:
    {
      name = t;
    }
    // lib.optionalAttrs (team ? privacy) { inherit (team) privacy; }
  ) teams;
  teamMembers = lib.mapAttrs (t: team: {
    team_id = ref "github_team.${t}.id";
    members = map (p: {
      username = loginOf p;
      role = "member";
    }) (team.members or [ ]);
  }) teams;
  teamRepos = lib.listToAttrs (
    lib.concatLists (
      lib.mapAttrsToList (
        t: team:
        map (
          r:
          lib.nameValuePair "${t}_${repoKey r.repo}" {
            team_id = ref "github_team.${t}.id";
            repository = repoRef r.repo;
            inherit (r) permission;
          }
        ) (team.repos or [ ])
      ) teams
    )
  );

  actions = g.actions or null;
  actionsPermissions = lib.mapAttrs' (n: v: lib.nameValuePair n v) (
    lib.optionalAttrs (actions != null) {
      org = {
        enabled_repositories = actions.enabledRepositories or "all";
      }
      // lib.optionalAttrs (actions ? allowedActions) { allowed_actions = actions.allowedActions; }
      // lib.optionalAttrs (actions ? shaPinningRequired) {
        sha_pinning_required = actions.shaPinningRequired;
      };
    }
  );
  actionsSecrets = lib.mapAttrs (n: s: {
    secret_name = n;
    plaintext_value = (sopsRef s.sourceRef).expr;
    visibility = "selected";
    selected_repository_ids = map repoId (s.repos or [ ]);
  }) secrets;

  # ---- rulesets ----------------------------------------------------------
  rank = {
    free = 0;
    team = 1;
    enterprise = 2;
  };
  plan = g.plan or "free";
  rulesets = g.rulesets or { };
  rankOf =
    p: rank.${p} or (throw "${where}: plan \"${toString p}\" is not one of free, team, enterprise");
  allowed = lib.filterAttrs (_: rs: rankOf (rs.requiresPlan or "free") <= rankOf plan) rulesets;
  skipped = lib.filterAttrs (n: _: !(allowed ? ${n})) rulesets;
  renderRuleset = n: rs: {
    name = n;
    enforcement = rs.enforcement or "active";
    target = rs.target or "branch";
    conditions = [
      {
        ref_name = [
          {
            include = rs.refs or [ "~DEFAULT_BRANCH" ];
            exclude = [ ];
          }
        ];
        repository_name = [
          {
            include = if (rs.repositories or [ ]) == [ ] then [ "~ALL" ] else map (r: ghName (repoKey r)) rs.repositories;
            exclude = [ ];
          }
        ];
      }
    ];
    rules = [ (snakeDeep (rs.rules or { })) ];
  };

  orgResources = {
    github_membership = memberships;
    github_team = teamResources;
    github_team_members = teamMembers;
    github_team_repository = teamRepos;
  }
  // lib.optionalAttrs (g ? organization) {
    github_organization_settings.org = snakeKeys g.organization;
  }
  // lib.optionalAttrs (actionsPermissions != { }) {
    github_actions_organization_permissions = actionsPermissions;
  }
  // lib.optionalAttrs (secrets != { }) { github_actions_organization_secret = actionsSecrets; }
  // lib.optionalAttrs (allowed != { }) {
    github_organization_ruleset = lib.mapAttrs renderRuleset allowed;
  };

  nonEmpty = lib.filterAttrs (_: v: v != { });
in
{
  terraform.required_providers = {
    github = {
      source = "integrations/github";
      version = "6.13.0";
    };
  }
  // lib.optionalAttrs (sopsData != { }) {
    sops = {
      source = "carlpett/sops";
      version = "1.4.1";
    };
  };

  provider.github = provider;

  data = lib.optionalAttrs (sopsData != { }) { sops_file = sopsData; };

  resource = nonEmpty (
    {
      github_repository = lib.mapAttrs renderRepo repos;
      github_branch_default = branchDefaults;
      github_repository_environment = environments;
      github_repository_environment_deployment_policy = envPolicies;
    }
    // lib.optionalAttrs isOrg orgResources
  );

  locals = {
    fleet_forks = lib.attrNames (lib.filterAttrs (_: r: r.fork) repos);
    fleet_skipped_rulesets = lib.mapAttrsToList (
      n: rs: "${n}: requires plan ${rs.requiresPlan}, the organisation is on ${plan}"
    ) skipped;
    fleet_unrendered =
      deployKeys
      ++ map (p: "outside_collaborator:${p}") (lib.optionals isOrg (members.outside or [ ]))
      ++ lib.optionals isOrg (
        map (k: "actions.${k}") (
          lib.filter (k: actions != null && actions ? ${k}) [
            "runnerGroups"
            "variables"
            "workflowPermissions"
          ]
        )
      );
  };
}
