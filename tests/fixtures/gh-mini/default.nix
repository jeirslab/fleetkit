# Minimal fleet data for tests/github.sh: one estate "gh" with a git block
# (organisation on the free plan, app auth, three members, one team whose key
# is not a legal resource name, Actions
# settings with one secret, and one ruleset that needs the team plan), three
# repositories (private with a default branch and an environment, public,
# archived fork) and principals with github logins. Generic names only;
# nothing here is a real organisation, login or credential.
_: {
  fleet = {
    operators.principals = {
      alice = {
        kind = "person";
        github = "alice-example";
      };
      bob = {
        kind = "person";
        github = "bob-example";
      };
      carol = {
        kind = "person";
        github = "carol-example";
      };
    };

    estates.gh = {
      owner = "Gh";
      environments.prod.branch = "main";
      secrets = {
        backend = "sops";
        files.gh = {
          path = "secrets/gh.json";
          keys = [
            "github/app-id"
            "github/installation-id"
            "github/pem"
            "ci-token"
          ];
        };
      };
      git = {
        kind = "org";
        platform = "github";
        org = "example-org";
        plan = "free";
        auth = {
          kind = "app";
          appIdRef = "sops:gh/gh#github/app-id";
          installationIdRef = "sops:gh/gh#github/installation-id";
          pemRef = "sops:gh/gh#github/pem";
        };
        organization = {
          billingEmail = "billing@example.invalid";
          defaultRepositoryPermission = "read";
          membersCanCreateRepositories = false;
          webCommitSignoffRequired = true;
        };
        members = {
          admin = [ "alice" ];
          member = [
            "bob"
            "carol"
          ];
        };
        # A team nobody is in yet: the team is rendered, its member list is not.
        teams.empty = { privacy = "closed"; };
        # The dot is not legal in a resource name: rendered as core_devs.
        teams."core.devs" = {
          privacy = "closed";
          members = [ "bob" ];
          repos = [
            {
              repo = "gh/app";
              permission = "push";
            }
          ];
        };
        actions = {
          enabledRepositories = "all";
          allowedActions = "all";
          secrets.CI_TOKEN = {
            sourceRef = "sops:gh/gh#ci-token";
            repos = [ "gh/app" ];
          };
        };
        rulesets.protect-main = {
          requiresPlan = "team";
          enforcement = "active";
          target = "branch";
          refs = [ "~DEFAULT_BRANCH" ];
          repositories = [ "gh/app" ];
          rules.deletion = true;
        };
      };
    };

    repos.gh = {
      app = {
        visibility = "private";
        description = "The application.";
        defaultBranch = "main";
        features = {
          issues = true;
          wiki = false;
          projects = false;
        };
        merge = {
          commit = false;
          squash = true;
          rebase = false;
          deleteBranchOnMerge = true;
        };
        environments.prod = {
          estateEnvironment = "gh/prod";
          branches = [
            "main"
            "release/1.x"
          ];
        };
      };
      site = {
        name = "site-public";
        visibility = "public";
        description = "The public site.";
      };
      mirror = {
        visibility = "public";
        fork = true;
        archived = true;
      };
    };
  };
}
