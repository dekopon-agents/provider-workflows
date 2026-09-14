#!/usr/bin/env bash
# Build a Dekopon provider component reproducibly. Run from the provider's repository root:
#
#     ../provider-workflows/build.sh            # writes <name>-provider.wasm + .sha256 here
#
# This is dekopon's examples/providers/build-component.sh harness, as ported by gh: a rustc proxy
# that normalizes `-C metadata` under one salt, `--remap-path-prefix` for the three local roots,
# and codegen-units=1. Provenance attestation proves who built an artifact, reproducibility proves
# what it was built from, and a release ships with both.
#
# The salt is `dekopon-provider-repro-v1` for every provider. It already includes the package name
# and version, so it cannot collide, and keeping it means this harness reproduces what the dekopon
# tree shipped byte for byte when fed the same source. Do not change it casually.
#
# Everything provider-specific is read from the provider's checkout, never passed in:
#   rust-toolchain.toml   the channel (and the sysroot remap string, so bytes match across machines)
#   Cargo.toml            exactly one package named dekopon-<name>-provider; its [profile.release]
#   rustflags             optional; wasm-only flags appended to the reproducibility set (turso)
set -euo pipefail

root=$PWD
[[ -f "$root/Cargo.toml" ]] || {
  echo "error: run from a provider repository root (no Cargo.toml in $root)" >&2
  exit 1
}
manifest="$root/Cargo.toml"
target_root=${CARGO_TARGET_DIR:-"$root/target"}
mkdir -p "$target_root"
target_root=$(cd "$target_root" && pwd -P)

metadata_domain="dekopon-provider-repro-v1"

# The toolchain is whatever rust-toolchain.toml declares; rustup installs it on first use.
rust_toolchain=$(sed -n 's/^channel *= *"\(.*\)"$/\1/p' "$root/rust-toolchain.toml" | head -1)
[[ -n "$rust_toolchain" ]] || {
  echo "error: rust-toolchain.toml must declare a channel" >&2
  exit 1
}
command -v rustup >/dev/null 2>&1 || {
  echo "error: rustup is required" >&2
  exit 1
}
rustup run "$rust_toolchain" rustc --version >/dev/null 2>&1 || {
  echo "error: Rust $rust_toolchain is not installed (rustup toolchain install $rust_toolchain)" >&2
  exit 1
}

# wasm-tools encodes the component, so its version is part of the bytes. The one source for the
# version is the shared ci.yml env; read it from there when the caller has not exported it.
shared_root=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
required_wasm_tools_version=${WASM_TOOLS_VERSION:-$(sed -n 's/^  WASM_TOOLS_VERSION: "\(.*\)"$/\1/p' "$shared_root/.github/workflows/ci.yml" | head -1)}
[[ -n "$required_wasm_tools_version" ]] || {
  echo "error: could not determine the required wasm-tools version" >&2
  exit 1
}
command -v wasm-tools >/dev/null 2>&1 || {
  echo "error: wasm-tools $required_wasm_tools_version is required" >&2
  exit 1
}
actual_wasm_tools=$(wasm-tools --version)
actual_wasm_tools_version=${actual_wasm_tools#wasm-tools }
actual_wasm_tools_version=${actual_wasm_tools_version%% *}
if [[ "$actual_wasm_tools_version" != "$required_wasm_tools_version" ]]; then
  echo "error: expected wasm-tools $required_wasm_tools_version, found $actual_wasm_tools" >&2
  exit 1
fi

# Exactly one package is the provider: dekopon-<name>-provider. Everything is named from it.
# (a while-read loop, not mapfile: macOS ships bash 3.2 and this runs on laptops too)
provider_packages=()
while IFS= read -r candidate; do
  provider_packages+=("$candidate")
done < <(
  rustup run "$rust_toolchain" cargo metadata --locked --no-deps --format-version 1 --manifest-path "$manifest" \
    | jq -r '.packages[].name | select(test("^dekopon-.+-provider$"))'
)
if [[ ${#provider_packages[@]} -ne 1 ]]; then
  echo "error: expected exactly one package named dekopon-<name>-provider, found: ${provider_packages[*]:-none}" >&2
  exit 1
fi
package=${provider_packages[0]}
name=${package#dekopon-}
name=${name%-provider}
core="$target_root/wasm32-unknown-unknown/release/${package//-/_}.wasm"
component=${1:-"$root/$name-provider.wasm"}

# Optional wasm-only flags. turso_core reaches for entropy through three getrandom majors, none
# of which can be configured from Cargo.toml; the host steps must not see these flags.
extra_rustflags=()
if [[ -f "$root/rustflags" ]]; then
  read -r -a extra_rustflags <<<"$(cat "$root/rustflags")"
fi

cargo_home=${CARGO_HOME:-"$HOME/.cargo"}
cargo_home=$(cd "$cargo_home" && pwd -P)
sysroot=$(rustup run "$rust_toolchain" rustc --print sysroot)
sysroot=$(cd "$sysroot" && pwd -P)
rustc_path=$(rustup which --toolchain "$rust_toolchain" rustc)
rustc_proxy="$target_root/deterministic-rustc"
cat >"$rustc_proxy" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

actual_rustc=${DEKOPON_BUILD_RUSTC:?}
source_root=${DEKOPON_BUILD_SOURCE_ROOT:?}
metadata_domain=${DEKOPON_BUILD_METADATA_DOMAIN:?}
manifest_dir=${CARGO_MANIFEST_DIR-}
repository_crate=false
if [[ "$manifest_dir" == "$source_root" || "$manifest_dir" == "$source_root/"* ]]; then
  repository_crate=true
fi

target=host
expect_target=false
for argument in "$@"; do
  if [[ "$expect_target" == true ]]; then
    target=$argument
    expect_target=false
    continue
  fi
  case $argument in
    --target) expect_target=true ;;
    --target=*) target=${argument#--target=} ;;
  esac
done

normalize_metadata=$repository_crate
if [[ "$target" == wasm32-unknown-unknown ]]; then
  normalize_metadata=true
fi

args=()
crate_name=
while (($#)); do
  case $1 in
    --crate-name)
      crate_name=$2
      args+=("$1" "$2")
      shift 2
      ;;
    --target)
      target=$2
      args+=("$1" "$2")
      shift 2
      ;;
    --target=*)
      target=${1#--target=}
      args+=("$1")
      shift
      ;;
    -C)
      if (($# >= 2)) && [[ $2 == metadata=* ]] && [[ "$normalize_metadata" == true ]]; then
        shift 2
      else
        args+=("$1")
        shift
      fi
      ;;
    -Cmetadata=*)
      if [[ "$normalize_metadata" == true ]]; then
        shift
      else
        args+=("$1")
        shift
      fi
      ;;
    *)
      args+=("$1")
      shift
      ;;
  esac
done

if [[ "$normalize_metadata" == true && -n "$crate_name" && -n "${CARGO_PKG_NAME-}" && -n "${CARGO_PKG_VERSION-}" ]]; then
  args+=(
    -C
    "metadata=$metadata_domain-${CARGO_PKG_NAME}-${CARGO_PKG_VERSION}-$crate_name-$target"
  )
fi
exec "$actual_rustc" "${args[@]}"
EOF
chmod 0700 "$rustc_proxy"

rustflags=(
  "--remap-path-prefix=$root=/dekopon/source"
  "--remap-path-prefix=$cargo_home=/dekopon/cargo"
  "--remap-path-prefix=$sysroot=/dekopon/rust/$rust_toolchain"
  '--cfg=dekopon_provider_repro_v1'
  '--check-cfg=cfg(dekopon_provider_repro_v1)'
  '-Ccodegen-units=1'
)
if ((${#extra_rustflags[@]})); then
  rustflags+=("${extra_rustflags[@]}")
fi
encoded_rustflags=$(printf '%s\x1f' "${rustflags[@]}")
encoded_rustflags=${encoded_rustflags%$'\x1f'}

rustup target add --toolchain "$rust_toolchain" wasm32-unknown-unknown
CARGO_ENCODED_RUSTFLAGS="$encoded_rustflags" \
  DEKOPON_BUILD_RUSTC="$rustc_path" \
  DEKOPON_BUILD_SOURCE_ROOT="$root" \
  DEKOPON_BUILD_METADATA_DOMAIN="$metadata_domain" \
  RUSTC="$rustc_proxy" \
  CARGO_TARGET_DIR="$target_root" \
  rustup run "$rust_toolchain" cargo build \
  --locked --manifest-path "$manifest" --package "$package" \
  --target wasm32-unknown-unknown --release
wasm-tools component new "$core" -o "$component"

for local_path in "$root" "$cargo_home" "$sysroot"; do
  if LC_ALL=C grep -aF -- "$local_path" "$component" >/dev/null; then
    echo "error: generated component embeds local build path: $local_path" >&2
    exit 1
  fi
done

if command -v sha256sum >/dev/null 2>&1; then
  hash=$(sha256sum "$component" | awk '{print $1}')
else
  hash=$(shasum -a 256 "$component" | awk '{print $1}')
fi
(
  cd "$(dirname "$component")"
  printf '%s  %s\n' "$hash" "$(basename "$component")" >"$(basename "$component").sha256"
)
printf 'generated %s (%s bytes, sha256 %s) with Rust %s and wasm-tools %s\n' \
  "$component" "$(wc -c <"$component" | tr -d ' ')" "$hash" "$rust_toolchain" "$required_wasm_tools_version"
