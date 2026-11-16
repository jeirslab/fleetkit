# Minimal fleet data for tests/google.sh: the estate "gcp" switches the Google
# add-on on (one project with three APIs and one project that disables an API
# when it is dropped, the credential a sops ref of its own secrets), and the
# estate "plain" declares nothing of it. Generic names only; nothing here is a
# real project or credential.
_: {
  fleet.estates = {
    gcp = {
      owner = "Gcp";
      environments.prod.branch = "main";
      secrets = {
        backend = "sops";
        files.hosts = {
          path = "secrets/hosts.yaml";
          keys = [ "integrations/google/main/deployer_key" ];
        };
      };
      addons.google = {
        enable = true;
        credentialRef = "sops:gcp/hosts#integrations/google/main/deployer_key";
        projects = {
          main = {
            projectId = "example-project-123456";
            services = [
              "drive.googleapis.com"
              "calendar-json.googleapis.com"
              "serviceusage.googleapis.com"
            ];
          };
          side = {
            projectId = "example-side-654321";
            services = [ "people.googleapis.com" ];
            disableOnDestroy = true;
          };
        };
      };
    };
    plain = {
      owner = "Plain";
      environments.prod.branch = "main";
    };
  };
}
