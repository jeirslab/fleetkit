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
# Every such name goes through tfName (a character outside [A-Za-z0-9_-]
# becomes "_"); two keys that end up with the same name are an error.
#
# Credentials are never literals: the provider's app_auth / token and every
# Actions secret value read the estate's secret refs (sops:<estate>/<file>#<key>)
# through data.sops_file, as lib/terraform.nix does (lib/sops-ref.nix).
#
# Repositories are never destroyed by removal: archive_on_destroy = true and
# lifecycle.prevent_destroy = true. A fork is rendered like any repository
# (the provider cannot create a fork relationship); the keys are listed in
# locals.fleet_forks.
#
# Per repository, besides the repository itself: github_actions_secret,
# github_actions_variable, github_issue_label and github_repository_file
# (repos.<estate>.<key>.actions.secrets / actions.variables / labels / files).
#
# What the render reports in locals instead of rendering:
#   fleet_unrendered            what the model holds but the kit cannot render:
#                               deploy keys, every environment's branches
#                               (those of a skipped environment too), outside
#                               collaborators, some git.actions blocks
#   fleet_skipped_rulesets      rulesets above git.plan
#   fleet_skipped_environments  environments of a private repository on plan
#                               "free" (an organisation or a personal account)
#   fleet_skipped_org_secrets   organisation secrets that select a private
#                               repository on plan "free" (the whole secret is
#                               dropped, and its sops data is not read)
#   fleet_runners               repos.<estate>.<key>.runners: data for a host
#                               module, never a resource
# An unset git.plan is "free". The plans are free < pro < team < enterprise;
# "pro" is a personal account's plan (GitHub Pro), never an organisation's.
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

  # A Terraform resource name: letters, digits, "_" and "-", not starting
  # with a digit or "-". Model keys are free text (a repo key "foo.bar"), so
  # every generated name goes through this.
  tfName =
    s:
    let
      clean = lib.concatMapStrings (c: if builtins.match "[A-Za-z0-9_-]" c != null then c else "_") (
        lib.stringToCharacters s
      );
    in
    if builtins.match "[A-Za-z_].*" clean != null then clean else "_${clean}";
  # [ { raw; value; label ? } ] -> { <tfName raw> = value; }; two entries that
  # render the same name are an error, never a silent overwrite. `label` is
  # what the error shows for an entry instead of its quoted raw name: a raw
  # name joined from two parts ("<repo key>_<item>") can be the same string
  # for both offenders, so those entries name their parts separately.
  named =
    what: pairs:
    let
      byName = lib.groupBy (p: tfName p.raw) pairs;
      clashes = lib.filterAttrs (_: ps: builtins.length ps > 1) byName;
    in
    if clashes != { } then
      throw "${where}: ${what}: ${
        lib.concatStringsSep "; " (
          lib.mapAttrsToList (
            n: ps: "${lib.concatMapStringsSep ", " (p: p.label or "\"${p.raw}\"") ps} all render the resource name \"${n}\""
          ) clashes
        )
      }"
    else
      lib.mapAttrs (_: ps: (builtins.head ps).value) byName;
  namedAttrs =
    what: f: attrs:
    named what (
      lib.mapAttrsToList (n: v: {
        raw = n;
        value = f n v;
      }) attrs
    );

  # Repository ids are <estate>/<key>; the key is the address, the GitHub name
  # is the repo's name or its key. The model only checks that a referenced
  # repository is declared, not that it is this estate's.
  repoKey =
    id:
    let
      k = lib.removePrefix "${estate}/" id;
    in
    if lib.hasPrefix "${estate}/" id && repos ? ${k} then
      k
    else
      throw "${where}: repository \"${id}\" is not a repository of estate \"${estate}\"";
  nameOf = k: repos.${k}.name or null;
  ghName = k: if nameOf k != null then nameOf k else k;
  repoAddr = k: "github_repository.${tfName k}";
  repoRef = id: ref "${repoAddr (repoKey id)}.name";
  repoId = id: ref "${repoAddr (repoKey id)}.repo_id";

  # ---- secrets -----------------------------------------------------------
  auth = g.auth or null;
  # Actions secrets are organisation resources: without an organisation none
  # is rendered, so its value is not read either.
  # An unset git.plan is "free": a paid organisation must say so to keep its
  # private repositories' environments and its organisation secrets, and a
  # personal account on GitHub Pro must say "pro" to keep the environments.
  plan = g.plan or "free";
  # free < pro < team < enterprise. "pro" is the paid plan of a personal
  # account; an organisation is on free, team or enterprise.
  rank = {
    free = 0;
    pro = 1;
    team = 2;
    enterprise = 3;
  };
  rankOf =
    p: rank.${p} or (throw "${where}: plan \"${toString p}\" is not one of free, pro, team, enterprise");
  planRank =
    if isOrg && plan == "pro" then
      throw "${where}.plan: \"pro\" is a personal account's plan; an organisation (kind = \"org\") is on free, team or enterprise"
    else
      rankOf plan;
  # What the reasons in locals call the owner of the plan.
  account = if isOrg then "the organisation" else "the account";
  # A private repository on GitHub Free cannot use organisation secrets, and
  # cannot have environments; the second holds for a personal account too
  # (environments in a private repository need GitHub Pro there), so it is
  # not tied to isOrg. Anything not public counts as private.
  isPrivate = k: (repos.${k}.visibility or "private") != "public";
  freePlan = planRank == rank.free;
  allOrgSecrets = lib.optionalAttrs isOrg (g.actions.secrets or { });
  orgSecretPrivateRepos =
    s:
    lib.filter (id: isPrivate (repoKey id)) (s.repos or [ ]);
  secretsSkipped = lib.filterAttrs (_: s: freePlan && orgSecretPrivateRepos s != [ ]) allOrgSecrets;
  secrets = lib.filterAttrs (n: _: !(secretsSkipped ? ${n})) allOrgSecrets;

  # Repository-level Actions settings, read with defaults: the options live in
  # modules/repos.nix.
  repoActions = r: r.actions or { };
  repoSecrets = lib.mapAttrs (_: r: (repoActions r).secrets or { }) repos;
  repoVariables = lib.mapAttrs (_: r: (repoActions r).variables or { }) repos;
  repoLabels = lib.mapAttrs (_: r: r.labels or { }) repos;
  repoFiles = lib.mapAttrs (_: r: r.files or { }) repos;
  repoRunners = lib.mapAttrs (_: r: r.runners or { }) repos;
  # [ { k; n; v; } ] for one per-repository attrset.
  flat =
    perRepo:
    lib.concatLists (lib.mapAttrsToList (k: m: lib.mapAttrsToList (n: v: { inherit k n v; }) m) perRepo);
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
    ++ lib.mapAttrsToList (_: s: s.sourceRef) secrets
    ++ map (x: x.v.sourceRef) (flat repoSecrets);
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
      archive_on_destroy = if r.archiveOnDestroy == null then true else r.archiveOnDestroy;
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
  branchDefaults = namedAttrs "default branches" (k: r: {
    repository = ref "${repoAddr k}.name";
    branch = r.defaultBranch;
  }) withDefaultBranch;

  allEnvList = lib.concatLists (
    lib.mapAttrsToList (
      k: r: lib.mapAttrsToList (env: e: { inherit k env e; }) r.environments
    ) repos
  );
  envSkipped = lib.filter (x: freePlan && isPrivate x.k) allEnvList;
  envList = lib.filter (x: !(freePlan && isPrivate x.k)) allEnvList;
  environments = named "repository environments" (
    map (x: {
      raw = "${x.k}_${x.env}";
      value = {
        repository = ref "${repoAddr x.k}.name";
        environment = x.env;
      };
    }) envList
  );
  # An environment's deployment branches are not rendered. A skipped
  # environment's branches are still declared and still not rendered, so they
  # stay listed (allEnvList, not envList).
  envBranches = lib.concatMap (
    x: map (b: "environment_branch:${x.k}.${x.env}:${b}") x.e.branches
  ) allEnvList;

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
  memberships = namedAttrs "members" (p: role: {
    username = loginOf p;
    inherit role;
  }) memberRole;

  teams = g.teams or { };
  teamResources = namedAttrs "teams" (
    t: team:
    {
      name = t;
    }
    // lib.optionalAttrs (team ? privacy) { inherit (team) privacy; }
  ) teams;
  teamMembers = namedAttrs "teams" (t: team: {
    team_id = ref "github_team.${tfName t}.id";
    members = map (p: {
      username = loginOf p;
      role = "member";
    }) team.members;
  }) (lib.filterAttrs (_: team: (team.members or [ ]) != [ ]) teams);
  teamRepos = named "team repositories" (
    lib.concatLists (
      lib.mapAttrsToList (
        t: team:
        map (r: {
          raw = "${t}_${repoKey r.repo}";
          value = {
            team_id = ref "github_team.${tfName t}.id";
            repository = repoRef r.repo;
            inherit (r) permission;
          };
        }) (team.repos or [ ])
      ) teams
    )
  );

  actions = g.actions or null;
  actionsPermissions = lib.mapAttrs' (n: v: lib.nameValuePair n v) (
    # Only when the estate declares the policy: enabled_repositories is the
    # one required argument, and inventing a value would change the org.
    lib.optionalAttrs (actions != null && actions ? enabledRepositories) {
      org = {
        enabled_repositories = actions.enabledRepositories;
      }
      // lib.optionalAttrs (actions ? allowedActions) { allowed_actions = actions.allowedActions; }
      // lib.optionalAttrs (actions ? shaPinningRequired) {
        sha_pinning_required = actions.shaPinningRequired;
      };
    }
  );
  actionsSecrets = namedAttrs "Actions secrets" (n: s: {
    secret_name = n;
    plaintext_value = (sopsRef s.sourceRef).expr;
    visibility = "selected";
    selected_repository_ids = map repoId (s.repos or [ ]);
  }) secrets;

  # ---- repository-level Actions, labels, files, runners ------------------
  repoName = k: ref "${repoAddr k}.name";
  # The resource name is "<repo key>_<item>", which does not say where the
  # key ends: repository "app" with "B_C" and repository "app_B" with "C" are
  # the same string. A clash is reported with the two parts apart.
  perRepoLabel = kind: x: "repository \"${x.k}\" ${kind} \"${x.n}\"";
  repoActionsSecrets = named "repository Actions secrets" (
    map (x: {
      raw = "${x.k}_${x.n}";
      label = perRepoLabel "Actions secret" x;
      value = {
        repository = repoName x.k;
        secret_name = x.n;
        plaintext_value = (sopsRef x.v.sourceRef).expr;
      };
    }) (flat repoSecrets)
  );
  repoActionsVariables = named "repository Actions variables" (
    map (x: {
      raw = "${x.k}_${x.n}";
      label = perRepoLabel "Actions variable" x;
      value = {
        repository = repoName x.k;
        variable_name = x.n;
        value = x.v;
      };
    }) (flat repoVariables)
  );
  issueLabels = named "issue labels" (
    map (x: {
      raw = "${x.k}_${x.n}";
      label = perRepoLabel "label" x;
      value = {
        repository = repoName x.k;
        name = x.n;
        inherit (x.v) color;
      }
      // lib.optionalAttrs ((x.v.description or null) != null) { inherit (x.v) description; };
    }) (flat repoLabels)
  );
  repositoryFiles = named "repository files" (
    map (x: {
      raw = "${x.k}_${x.n}";
      label = perRepoLabel "file" x;
      value = {
        repository = repoName x.k;
        file = x.n;
        inherit (x.v) content;
        overwrite_on_create = x.v.overwrite or true;
      }
      // lib.optionalAttrs ((x.v.branch or null) != null) { inherit (x.v) branch; }
      // lib.optionalAttrs ((x.v.message or null) != null) { commit_message = x.v.message; };
    }) (flat repoFiles)
  );
  # Terraform cannot register a runner: report the declared ones only.
  runnerReport = map (x: {
    repository = "${estate}/${x.k}";
    name = x.n;
    labels = x.v.labels or [ ];
    on = x.v.on or null;
  }) (flat repoRunners);

  # ---- rulesets ----------------------------------------------------------
  rulesets = g.rulesets or { };
  allowed = lib.filterAttrs (_: rs: rankOf (rs.requiresPlan or "free") <= planRank) rulesets;
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
    github_organization_ruleset = namedAttrs "rulesets" renderRuleset allowed;
  };

  nonEmpty = lib.filterAttrs (_: v: v != { });
in
# The plan is validated whatever the estate declares: forced here, not only
# where an environment, organisation secret or ruleset happens to read it.
builtins.seq planRank {
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
      github_repository = namedAttrs "repositories" renderRepo repos;
      github_branch_default = branchDefaults;
      github_repository_environment = environments;
      github_actions_secret = repoActionsSecrets;
      github_actions_variable = repoActionsVariables;
      github_issue_label = issueLabels;
      github_repository_file = repositoryFiles;
    }
    // lib.optionalAttrs isOrg orgResources
  );

  locals = {
    fleet_forks = lib.attrNames (lib.filterAttrs (_: r: r.fork) repos);
    fleet_skipped_rulesets = lib.mapAttrsToList (
      n: rs: "${n}: requires plan ${rs.requiresPlan}, the organisation is on ${plan}"
    ) skipped;
    fleet_skipped_environments = map (
      x: "${x.k}.${x.env}: private repository, environments need a paid plan, ${account} is on ${plan}"
    ) envSkipped;
    fleet_skipped_org_secrets = lib.mapAttrsToList (
      n: s:
      "${n}: organisation secrets are not available to private repositories on ${plan} (${
        lib.concatStringsSep ", " (orgSecretPrivateRepos s)
      }); use repository-level secrets"
    ) secretsSkipped;
    fleet_runners = runnerReport;
    fleet_unrendered =
      deployKeys
      ++ envBranches
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
