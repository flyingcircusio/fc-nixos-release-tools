{
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

  outputs = { self, nixpkgs, ... }:
    let
      supportedSystems = [ "x86_64-linux" "x86_64-darwin" "aarch64-linux" "aarch64-darwin" ];
      forAllSystems = nixpkgs.lib.genAttrs supportedSystems;
    in
    {
      packages = forAllSystems (system: let
        pkgs = nixpkgs.legacyPackages.${system};
        ppkgs = pkgs.python3.pkgs;
      in {
        default = ppkgs.buildPythonApplication {
          name = "fc-release";
          src = ./.;
          pyproject = true;
          build-system = [ ppkgs.setuptools-scm ];
          dependencies = [
            ppkgs.setuptools
            pkgs.scriv
            pkgs.gh
          ];
        };
      });

      devShells = forAllSystems (system: let
        pkgs = nixpkgs.legacyPackages.${system};
      in {
        default = pkgs.mkShell {
          packages = [ pkgs.scriv pkgs.gh ];
        };
      });
    };
}
