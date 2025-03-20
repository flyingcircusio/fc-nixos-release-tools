# Release tooling for fc-nixos

## Installation

> [!NOTE]
> We require experimental Nix features, thus you need the following settings in your `/etc/nix/nix.conf`:
> ```
> extra-experimental-features = nix-command flakes
> ```

```bash
$ nix build
```

## Usage
Use `./result/bin/release status` to show current state and possible actions.

Each command is atomic and can be interrupted.

Example usage:
```bash
./result/bin/release init
./result/bin/release add-branch 23.11
./result/bin/release add-branch 24.05
./result/bin/release test-branch 23.11
./result/bin/release test-branch 24.05
./result/bin/release doc
./result/bin/release tag
```
