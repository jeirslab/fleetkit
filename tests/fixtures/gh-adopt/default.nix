# Added to tests/fixtures/gh-mini by tests/pulumi.sh for the adoption ids: the
# organisation on the team plan (so the environment, the organisation secret
# and the ruleset are rendered), a team whose name is its slug with a member
# and a repository, and every per-repository block (secret, variable, labels,
# files with and without a branch, names with a ":" and a "${"). Generic
# names only.
{ lib, ... }:
{
  fleet = {
    estates.gh = {
      secrets.files.gh.keys = [ "repo-token" ];
      git = {
        plan = lib.mkForce "team";
        teams.platform = {
          privacy = "closed";
          members = [ "carol" ];
          repos = [
            {
              repo = "gh/site";
              permission = "pull";
            }
          ];
        };
      };
    };
    repos.gh.app = {
      actions = {
        secrets.REPO_TOKEN.sourceRef = "sops:gh/gh#repo-token";
        variables.DEPLOY_TARGET = "staging";
      };
      labels = {
        bug.color = "b60205";
        "kind: \${x}".color = "d93f0b";
      };
      files = {
        ".github/workflows/signal.yml".content = "name: signal\n";
        "docs/a:b.txt" = {
          content = "x";
          branch = "release/1.x";
        };
      };
    };
  };
}
