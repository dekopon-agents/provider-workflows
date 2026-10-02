# provider-workflows

The CI and release workflows every Dekopon provider calls. A provider repository
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

To release, commit the version bump to the provider's `main`, then push an annotated `vX.Y.Z` tag
on that commit. The tag runs the release workflow.

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
