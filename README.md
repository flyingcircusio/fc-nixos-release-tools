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
./result/bin/release start
./result/bin/release merge-production 23.11
./result/bin/release merge-production 24.05
./result/bin/release release-production 23.11
./result/bin/release release-production 24.05
./result/bin/release doc
./result/bin/release tag
```

## Hacking


### Colors

I've reviewed some advise on colours and ended up with this, to ensure light/dark mode compatibility:

Good choices:

* neutral (+bold)
* red (+bold)
* blue (+bold)
* purple (+bold)

Acceptable choices:

* green (-bold)
* cyan (+bold)

All others should be avoided.
