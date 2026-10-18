# provider-workflows

The CI, release and cut-release workflows every Dekopon provider calls (cut-release also serves
dekopon, dekopon-console, vm-runner and cm). A provider repository
owns its source, its `Cargo.toml`, its `rust-toolchain.toml`, its `deny.toml` and its `wit/`
mirrors; everything else — the toolchain install, the pinned tools, the caching, the lints, the
reproducible component build (its CI byte-for-byte rebuild is paused for speed), the SBOM, the
release, the GHCR push and the anonymous attestation verification — lives here, once, and every
caller tracks it at `@main`: a change to shared CI is one commit here.

## Callers

`.github/workflows/ci.yml`:

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:

permissions:
  contents: read

jobs:
  ci:
    uses: dekopon-agents/provider-workflows/.github/workflows/ci.yml@main
```

`.github/workflows/release.yml`:

```yaml
name: Release

on:
  push:
    tags:
      - "v*"

concurrency:
  group: release-${{ github.ref_name }}
  cancel-in-progress: false

jobs:
  release:
    uses: dekopon-agents/provider-workflows/.github/workflows/release.yml@main
    permissions:
      contents: write
      id-token: write
      attestations: write
      packages: write
```

`.github/workflows/cut-release.yml`:

```yaml
name: Cut release

on:
  workflow_dispatch:
    inputs:
      tags:
        description: Space-separated tags to create on one commit
        type: string
        required: true
      sha:
        description: The commit on main to tag; empty means main's HEAD
        type: string
        default: ""
      dry_run:
        description: Check and create the tags without pushing them
        type: boolean
        default: true

permissions:
  contents: read

jobs:
  cut:
    uses: dekopon-agents/provider-workflows/.github/workflows/cut-release.yml@main
    with:
      tags: ${{ inputs.tags }}
      sha: ${{ inputs.sha }}
      package: dekopon-<name>-provider
      dry_run: ${{ inputs.dry_run }}
    secrets:
      DEKOPON_RELEASE_APP_ID: ${{ secrets.DEKOPON_RELEASE_APP_ID }}
      DEKOPON_RELEASE_APP_KEY: ${{ secrets.DEKOPON_RELEASE_APP_KEY }}
```

To release, merge the version bump to the provider's `main`, then dispatch Cut release with
`tags: vX.Y.Z` (`gh workflow run cut-release.yml -f tags=vX.Y.Z -f dry_run=false`). It refuses a commit
off `main`, an existing tag, or a tag whose version differs from `package`'s at that commit, then pushes
an annotated tag as the dekopon-release App, whose push starts the release workflow (a `GITHUB_TOKEN`
push would not). `require_head: true` additionally refuses anything but `main`'s HEAD. Pushing an
annotated tag by hand still works.

## The provider's half of the contract

- `rust-toolchain.toml` — channel, `components`, `targets = ["wasm32-unknown-unknown"]`. The
  workflow installs exactly this and exports it as `RUSTUP_TOOLCHAIN`.
- `Cargo.toml` — exactly one package named `dekopon-<name>-provider`, with its own
  `[profile.release]`. That profile is part of the shipped bytes.
- `deny.toml` — the dependency policy `cargo deny check bans licenses sources advisories` runs.
- `wit/` — provider-owned WIT dependencies; the provider's `cargo test` runs SDK conformance
  against the built component, and the broker refuses components it cannot link at load.
- `rustflags` — optional, one line, wasm-only flags appended to the reproducibility set.
- Tests read the component path from `DEKOPON_PROVIDER_COMPONENT` and must fail (never skip,
  never return early) when it is unset. The workflow exports it after the build.

## Naming, all derived from the repository name

| Repository | `dekopon-agents/dekopon-provider-<name>` |
| --- | --- |
| Package | `dekopon-<name>-provider` |
| Component | `<name>-provider.wasm` (plus `<name>-provider.wasm.sha256`) |
| SBOM | `<name>-provider.cdx.json` |
| OCI | `ghcr.io/dekopon-agents/provider-<name>` |

The CI check is `ci / validate`.

## Build locally

Clone this repository next to the provider, then from the provider root:

```
../provider-workflows/build.sh
```

It needs `rustup`, `jq`, and the `wasm-tools` version `.github/workflows/ci.yml` pins. It writes
`<name>-provider.wasm` and its `.sha256` into the provider root — the same bytes CI produces.
