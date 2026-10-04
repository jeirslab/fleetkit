# nixpkgs overlay for the llm-serving add-on: Olah (a Hugging Face pull-through
# cache) and fastapi-utils (a helper it imports), both from PINNED PyPI sdists.
#
# Adds `pkgs.olah` (an application) and `fastapi-utils` to every python
# package set (via pythonPackagesExtensions, so `python3Packages.fastapi-utils`). ./default.nix applies this overlay
# itself (module-level `nixpkgs.overlays`) when the cold store is enabled; a
# consumer who wants the packages elsewhere can use
#   nixpkgs.overlays = [ (import <fleetkit>/add-ons/llm-serving/overlays.nix) ];
#
# Each quirk below was learned by experiment; do not "clean up" without
# re-running the gate (`nix build .#checks.<system>.addon-llm-serving`).
final: prev: {
  pythonPackagesExtensions = (prev.pythonPackagesExtensions or [ ]) ++ [
    (pyFinal: pyPrev: {

      # (a) fastapi-utils is NOT in nixpkgs, and Olah imports
      #     `fastapi_utils.tasks.repeat_every`, so it has to be packaged here.
      fastapi-utils = pyFinal.buildPythonPackage rec {
        pname = "fastapi-utils";
        version = "0.8.0";
        pyproject = true;

        src = pyFinal.fetchPypi {
          pname = "fastapi_utils";
          inherit version;
          # sha256 hex eca834e80c09f85df30004fe5e861981262b296f60c93d5a1a1416fe4c784140
          hash = "sha256-7Kg06AwJ+F3zAAT+XoYZgSYrKW9gyT1aGhQW/kx4QUA=";
        };

        build-system = [ pyFinal.poetry-core ];
        # (b) typing-inspect is declared upstream only as an *extra* but
        #     imported unconditionally (fastapi_utils/cbv.py), so it must be a
        #     real dependency or the import fails at runtime.
        dependencies = with pyFinal; [ fastapi pydantic psutil typing-inspect ];
        # (c) nixpkgs carries other majors than the declared ranges (psutil<6
        #     and friends); only `tasks.repeat_every` is used downstream.
        pythonRelaxDeps = true;
        doCheck = false;
        # What catches (b) and (c) going wrong.
        pythonImportsCheck = [ "fastapi_utils.tasks" "fastapi_utils.cbv" ];

        meta = {
          description = "Reusable utilities for FastAPI (here: the periodic-task decorator Olah imports)";
          homepage = "https://github.com/dmontagu/fastapi-utils";
          license = final.lib.licenses.mit;
        };
      };
    })
  ];

  # An application, not a library: python package sets reject applications
  # ("should use buildPythonPackage or toPythonModule"), so it lives at the top
  # level and is built from the python set that carries fastapi-utils.
  olah = final.python3Packages.buildPythonApplication rec {
    pname = "olah";
    version = "0.5.1";
    pyproject = true;

    src = final.python3Packages.fetchPypi {
      inherit pname version;
      # sha256 hex 27b8098e567cd802f4828dee61679de5dd6cbd733b8fb7cfeaf4084d82c41ff7
      hash = "sha256-J7gJjlZ82AL0go3uYWed5d1svXM7j7fP6vQITYLEH/c=";
    };

    build-system = [ final.python3Packages.setuptools ];
    dependencies = with final.python3Packages; [
      fastapi fastapi-utils httpx pydantic pydantic-settings requests toml rich
      shortuuid uvicorn tenacity pytz gitpython pyyaml typing-inspect
      huggingface-hub jinja2 python-multipart portalocker peewee brotli
    ];
    # (c) Upstream version floors are ahead of nixpkgs (fastapi>=0.141 vs
    #     0.136, ...). Relax them all; pythonImportsCheck below, plus the
    #     runtime smoke test in checks.nix, is what catches real breakage.
    pythonRelaxDeps = true;
    doCheck = false;
    pythonImportsCheck = [ "olah.server" ];

    meta = {
      description = "Self-hosted Hugging Face mirror (pull-through cache)";
      homepage = "https://github.com/vtuber-plan/olah";
      license = final.lib.licenses.mit;
      # (d) The entry point is `olah-cli`, not `olah`.
      mainProgram = "olah-cli";
    };
  };
}
