#!/bin/sh
# Install a complete ENGRAI CLI release. No Python, Git, uv, or sudo required.
set -eu

fail() { printf 'ENGRAI installation failed: %s\n' "$*" >&2; exit 1; }
usage() {
    cat <<'EOF'
Usage: install.sh [--version TAG] [--backend BACKEND] [--prefix DIR]
                  [--bin-dir DIR] [--no-modify-path]

Downloads ENGRAI with its text and image engines. Defaults to the latest
published release, Metal on Apple Silicon, and CPU on other supported hosts.
Only published platform/backend combinations can be installed.
Updates replace the app command and preserve all saved data and models.
EOF
}

release=latest
backend=
prefix="${XDG_DATA_HOME:-$HOME/.local/share}/engrai-server/app"
bin_dir="$HOME/.local/bin"
modify_path=1
while [ "$#" -gt 0 ]; do
    case "$1" in
        --version|--backend|--prefix|--bin-dir)
            [ "$#" -ge 2 ] || fail "missing value for $1"
            case "$1" in
                --version) release=$2 ;;
                --backend) backend=$2 ;;
                --prefix) prefix=$2 ;;
                --bin-dir) bin_dir=$2 ;;
            esac
            shift 2 ;;
        --no-modify-path) modify_path=0; shift ;;
        --help|-h) usage; exit 0 ;;
        *) fail "unknown option: $1" ;;
    esac
done
case "$release" in ''|*[!A-Za-z0-9._+-]*) fail "invalid release tag" ;; esac
case "$(uname -s)" in Darwin) system=macos ;; Linux) system=linux ;; *) fail "supported systems: macOS and Linux" ;; esac
case "$(uname -m)" in arm64|aarch64) arch=aarch64 ;; x86_64|amd64) arch=x86_64 ;; *) fail "supported architectures: ARM64 and x86-64" ;; esac
# Detect Apple Silicon even when the shell is running under Rosetta.
if [ "$system" = macos ] && [ "$(sysctl -n hw.optional.arm64 2>/dev/null || true)" = 1 ]; then arch=aarch64; fi
if [ -z "$backend" ]; then
    backend=cpu
    if [ "$system-$arch" = macos-aarch64 ]; then backend=metal; fi
fi
case "$backend" in cpu|metal|cuda|rocm|vulkan) ;; *) fail "unknown backend: $backend" ;; esac
if [ "$backend" = metal ] && [ "$system" != macos ]; then fail "Metal requires macOS"; fi
case "$prefix:$bin_dir" in *'
'*) fail "installation paths cannot contain newlines" ;; esac
case "$prefix" in /*) ;; *) fail "--prefix must be an absolute path" ;; esac
case "$bin_dir" in /*) ;; *) fail "--bin-dir must be an absolute path" ;; esac
for utility in curl tar awk sed; do command -v "$utility" >/dev/null 2>&1 || fail "$utility is required"; done
if command -v sha256sum >/dev/null 2>&1; then hash_tool=sha256sum
elif command -v shasum >/dev/null 2>&1; then hash_tool=shasum
else fail "sha256sum or shasum is required"; fi

mkdir -p "$prefix/releases" "$bin_dir"
mkdir "$prefix/.install-lock" 2>/dev/null || fail "another install is running (lock: $prefix/.install-lock)"
temporary=
launcher=
cleanup() {
    [ -z "$temporary" ] || rm -rf "$temporary"
    [ -z "$launcher" ] || rm -f "$launcher"
    rmdir "$prefix/.install-lock" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP
temporary=$(mktemp -d "$prefix/.download-XXXXXX")
asset="engrai-server-$system-$arch-$backend.tar.gz"
base=https://github.com/va-desai-dev/ENGRAISERVER/releases
if [ "$release" = latest ]; then base="$base/latest/download"
else base="$base/download/$release"; fi
download() { curl --fail --location --silent --show-error --proto '=https' --tlsv1.2 --retry 3 "$1" -o "$2"; }
printf 'Downloading ENGRAI for %s/%s (%s), including text and image engines…\n' "$system" "$arch" "$backend"
download "$base/$asset.sha256" "$temporary/checksum" || fail "no published $release bundle for $system/$arch/$backend"
download "$base/$asset" "$temporary/bundle.tar.gz" || fail "could not download the release bundle"
expected=$(awk 'NR == 1 {print $1}' "$temporary/checksum")
case "$expected" in ''|*[!0-9a-f]*) fail "invalid release checksum" ;; esac
[ "${#expected}" -eq 64 ] || fail "invalid release checksum"
if [ "$hash_tool" = sha256sum ]; then actual=$(sha256sum "$temporary/bundle.tar.gz" | awk '{print $1}')
else actual=$(shasum -a 256 "$temporary/bundle.tar.gz" | awk '{print $1}'); fi
[ "$actual" = "$expected" ] || fail "download checksum mismatch; existing installation was not changed"
# Release archives contain a single root and regular files/directories only.
# Reject traversal and links before extraction, including a checksum-valid archive.
tar -tzf "$temporary/bundle.tar.gz" > "$temporary/members"
awk '/^\// || /(^|\/)\.\.($|\/)/ || !/^engrai-server(\/|$)/ {bad=1} END {exit bad}' "$temporary/members" || fail "unsafe archive paths"
tar -tvzf "$temporary/bundle.tar.gz" > "$temporary/types"
awk 'substr($0,1,1) != "-" && substr($0,1,1) != "d" {bad=1} END {exit bad}' "$temporary/types" || fail "archive contains unsupported links or special files"
tar -xzf "$temporary/bundle.tar.gz" -C "$temporary"
candidate="$temporary/engrai-server"
[ -x "$candidate/engrai-server" ] || fail "archive does not contain the CLI"
for engine in engrai-text engrai-image; do
    [ -f "$candidate/runtimes/$engine/active.json" ] || fail "archive is missing its $engine runtime"
done
# The destination is immutable once selected; retained older releases let an
# already-running gateway finish without replacing the binaries it is using.
destination="$prefix/releases/$expected"
if [ ! -d "$destination" ]; then mv "$candidate" "$destination"; fi
"$destination/engrai-server" --version
for engine in text image; do
    "$destination/engrai-server" runtime status --runtime "$engine" \
        --runtime-dir "$destination/runtimes" --json >/dev/null || fail "embedded $engine engine failed verification"
done
"$destination/engrai-server" init

quote() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }
launcher=$(mktemp "$bin_dir/.engrai-server-XXXXXX")
{
    printf '#!/bin/sh\n# ENGRAI managed launcher\nENGRAI_CLI_LAUNCHER='
    quote "$bin_dir/engrai-server"
    printf '\nexport ENGRAI_CLI_LAUNCHER\nexec '
    quote "$destination/engrai-server"
    printf ' "$@"\n'
} > "$launcher"
chmod 755 "$launcher"
[ ! -d "$bin_dir/engrai-server" ] || fail "$bin_dir/engrai-server is a directory"
mv -f "$launcher" "$bin_dir/engrai-server"
launcher=

# Only add PATH when needed. Never replace an existing shell configuration.
case ":$PATH:" in *":$bin_dir:"*) on_path=1 ;; *) on_path=0 ;; esac
if [ "$on_path" = 0 ] && [ "$modify_path" = 1 ]; then
    path_line="export PATH=$(quote "$bin_dir"):\"\$PATH\""
    case "${SHELL:-/bin/sh}" in
        */zsh) profile="${ZDOTDIR:-$HOME}/.zshrc" ;;
        */bash)
            if [ "$system" = macos ]; then profile="$HOME/.bash_profile"
            else profile="$HOME/.bashrc"; fi ;;
        */sh|*/dash|*/ksh) profile="$HOME/.profile" ;;
        *) profile= ;;
    esac
    if [ -n "$profile" ]; then
        mkdir -p "$(dirname "$profile")"
        if ! grep -Fqx "$path_line" "$profile" 2>/dev/null; then
            printf '\n# ENGRAI CLI\n%s\n' "$path_line" >> "$profile"
        fi
        printf 'Added ENGRAI to PATH in %s. Open a new terminal to use engrai-server.\n' "$profile"
    fi
fi
printf '\nENGRAI is installed. Start it with:\n  '
if [ "$on_path" = 1 ]; then printf 'engrai-server serve\n'
else quote "$bin_dir/engrai-server"; printf ' serve\n'; fi
printf '\nOpen http://127.0.0.1:8400 and choose a model. Both engines are included; model weights download separately.\n'
printf 'To update, stop ENGRAI, rerun this installer, and start it again. Saved data is preserved.\n'
