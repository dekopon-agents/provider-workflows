# provider-workflows

The CI, release and cut-release workflows every Dekopon provider calls. A provider repository
owns its source, its `Cargo.toml`, its `rust-toolchain.toml`, its `deny.toml` and its `wit/`
mirrors; everything else — the toolchain install, the pinned tools, the caching, the lints, the
reproducible component build (its CI byte-for-byte rebuild is paused for speed), the component
inspection, the SBOM, the release, the GHCR push and the anonymous attestation verification —
lives here, once, and is pinned by SHA from each caller.
Dependabot bumps the pin when a new `vN` tag lands.

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
    uses: dekopon-agents/provider-workflows/.github/workflows/ci.yml@SHARED_SHA # vN
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
    uses: dekopon-agents/provider-workflows/.github/workflows/release.yml@SHARED_SHA # vN
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
      version:
        description: The version to release, without the v (1.2.3 or 1.2.3-rc.1)
        type: string
        required: true

jobs:
  cut:
    uses: dekopon-agents/provider-workflows/.github/workflows/cut-release.yml@SHARED_SHA # vN
    permissions:
      contents: write
    with:
      version: ${{ inputs.version }}
    secrets: inherit
```

`SHARED_SHA` is a commit of this repository — pin to a commit; Dependabot bumps it. `vN` is the tag
on that commit, and the trailing comment is what lets Dependabot recognize the pin, so keep both.
Add `.github/dependabot.yml` (`github-actions`, `/`, weekly) alongside.

## The provider's half of the contract

- `rust-toolchain.toml` — channel, `components`, `targets = ["wasm32-unknown-unknown"]`. The
  workflow installs exactly this and exports it as `RUSTUP_TOOLCHAIN`.
- `Cargo.toml` — exactly one package named `dekopon-<name>-provider`, with its own
  `[profile.release]`. That profile is part of the shipped bytes.
- `deny.toml` — the dependency policy `cargo deny check bans licenses sources advisories` runs.
- `wit/` — `wit/deps/*.wit` must be byte-identical to the same-named file shipped by a resolved
  `dekopon-provider-*` crate. A mirror with no upstream fails.
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
