# Adoption ids: for each Terraform resource type the kit renders, the id the
# pinned provider imports an existing resource by. Evaluation only.
#
#   import ./adopt.nix { lib; } -> { proxmox; github; }
#     proxmox      the map for lib.internal.guests renders
#     github tf    the map for one lib.internal.github render (`tf`)
#
# A map is what toPulumi takes as `adopt`:
#   <terraform type> = name: args: "<import id>" | { unresolved = "<why>"; }
# with `args` the resource's rendered Terraform arguments. An id is data beside
# the program (resources.<key>.adopt, the stack's adoptIds): `fleetkit adopt`
# uses it once, and it is never rendered into a program (fleetkit#61: an
# `import` left in a program destroyed an adopted container on the next `up`).
#
# An id that the model does not determine is never guessed: the rule returns
# { unresolved = "<why, and what value is needed>"; } and the stack lists the
# resource in adoptUnresolved, for the command to ask for.
{ lib }:
let
  # ── bpg/proxmox ────────────────────────────────────────────────────────
  guest = _: a: "${a.node_name}/${toString a.vm_id}";
  proxmox = {
    # <node>/<vmid>
    proxmox_virtual_environment_container = guest;
    proxmox_virtual_environment_vm = guest;
    # <pool_id>
    proxmox_virtual_environment_pool = _: a: a.pool_id;
  };

  # ── integrations/github 6.13.0 ─────────────────────────────────────────
  # Every format below is from the provider at tag v6.13.0:
  #   docs:   https://github.com/integrations/terraform-provider-github/blob/v6.13.0/docs/resources/<name>.md  ("Import")
  #   source: https://github.com/integrations/terraform-provider-github/blob/v6.13.0/github/resource_github_<name>.go  (Importer)
  # cited per rule as docs/<name>.md and resource_github_<name>.go.
  github =
    tf:
    let
      res = tf.resource or { };
      org = tf.provider.github.owner;

      # The render escapes estate text for Terraform ("${" as "$${", "%{" as
      # "%%{", lib/github.nix tfText); an id is the text itself.
      plain =
        lib.replaceStrings
          [
            "$\${"
            "%%{"
          ]
          [
            "\${"
            "%{"
          ];
      # util.go escapeIDPart / unescapeIDPart: a ":" inside a part is "??".
      esc = lib.replaceStrings [ ":" ] [ "??" ];

      # "${github_repository.<n>.name}" -> that repository's GitHub name.
      repoOf =
        s:
        let
          m = builtins.match "\\$\\{github_repository\\.([A-Za-z0-9_-]+)\\.name}" s;
        in
        if m == null then plain s else res.github_repository.${lib.head m}.name;

      # A team is imported by its numeric id or its slug (util_team.go
      # getTeamID: an integer is an id, anything else is looked up as a slug).
      # The model has the team's name. GitHub derives the slug from the name
      # (lower case, other characters to "-"); only a name that is already in
      # slug form is certainly its own slug, and one that is all digits would
      # be read as an id.
      slugOf =
        name:
        if builtins.match "[a-z0-9]+(-[a-z0-9]+)*" name != null && builtins.match "[0-9]+" name == null then
          name
        else
          null;
      teamWhy =
        name:
        "the team's slug or numeric id (GET /orgs/${org}/teams); the name \"${name}\" is not certainly its slug";
      # "${github_team.<n>.id}" -> the team's name.
      teamNameOf =
        s:
        let
          m = builtins.match "\\$\\{github_team\\.([A-Za-z0-9_-]+)\\.id}" s;
        in
        if m == null then plain s else res.github_team.${lib.head m}.name;
    in
    {
      # docs/repository.md: "Repositories can be imported using the `name`".
      github_repository = _: a: a.name;

      # docs/branch_default.md: `terraform import github_branch_default.branch_default my-repo`;
      # resource_github_branch_default.go: the id is the repository name.
      github_branch_default = _: a: repoOf a.repository;

      # docs/membership.md: "an ID made up of `organization:username`".
      github_membership = _: a: "${org}:${a.username}";

      # docs/team.md: "the GitHub team ID or name"; resource_github_team.go
      # passes the id to getTeamID, which reads a non-integer as the slug.
      github_team =
        _: a:
        if slugOf a.name != null then
          slugOf a.name
        else
          { unresolved = "adopt = ${teamWhy a.name}"; };

      # docs/team_members.md: "either by the team slug or team ID";
      # resource_github_team_members.go resolves either to the team.
      github_team_members =
        _: a:
        let
          n = teamNameOf a.team_id;
        in
        if slugOf n != null then
          slugOf n
        else
          { unresolved = "adopt = ${teamWhy n}"; };

      # docs/team_repository.md: "`team_id:repository` or `team_name:repository`";
      # resource_github_team_repository.go: parseID2, the first part through
      # getTeamID (id or slug).
      github_team_repository =
        _: a:
        let
          n = teamNameOf a.team_id;
          repo = repoOf a.repository;
        in
        if slugOf n != null then
          "${slugOf n}:${repo}"
        else
          { unresolved = "adopt = \"<team>:${repo}\", <team> being ${teamWhy n}"; };

      # docs/organization_settings.md: "using the `id` of the organization",
      # a number the model does not hold.
      github_organization_settings = _: _: {
        unresolved = "adopt = the organisation's numeric id (GET /orgs/${org}, field id)";
      };

      # docs/actions_organization_permissions.md: "using the name of the
      # GitHub organization".
      github_actions_organization_permissions = _: _: org;

      # docs/repository_environment.md: "the repository name and environment
      # name (any `:` in the environment name need to be escaped as `??`)
      # separated by a `:`".
      github_repository_environment = _: a: "${repoOf a.repository}:${esc (plain a.environment)}";

      # docs/organization_ruleset.md: "using the GitHub ruleset ID";
      # resource_github_organization_ruleset.go parses it as an integer. GitHub
      # assigns it; the model has the ruleset's name only.
      github_organization_ruleset = _: a: {
        unresolved = "adopt = the numeric id of the ruleset named \"${a.name}\" (GET /orgs/${org}/rulesets)";
      };

      # docs/issue_label.md: "an ID made up of `repository:name`";
      # resource_github_issue_label.go: parseID2, the label name is the rest
      # (not escaped).
      github_issue_label = _: a: "${repoOf a.repository}:${plain a.name}";

      # docs/actions_variable.md: "the repository name, and variable name
      # separated by a `:`".
      github_actions_variable = _: a: "${repoOf a.repository}:${a.variable_name}";

      # docs/repository_file.md: "`repo`, `file path` (any `:` in the file
      # path need to be escaped as `??`) and `branch` or empty branch for the
      # default branch", e.g. `example:.gitignore:feature-branch`,
      # `example:.gitignore:`.
      github_repository_file =
        _: a: "${repoOf a.repository}:${esc (plain a.file)}:${if a ? branch then plain a.branch else ""}";

      # docs/actions_secret.md: "the repository name, and secret name separated
      # by a `:`". The import cannot read the value ("the `value`, ...
      # `plaintext_value` fields will not be populated in the state"), so the
      # run after an adoption writes the model's value: an update, never "same".
      github_actions_secret = _: a: "${repoOf a.repository}:${a.secret_name}";

      # docs/actions_organization_secret.md: "using the secret name as the
      # ID"; the value is not read, as above.
      github_actions_organization_secret = _: a: a.secret_name;
    };
in
{
  inherit proxmox github;
}
