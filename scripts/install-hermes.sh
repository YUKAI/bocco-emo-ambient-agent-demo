#!/bin/sh
# Install Hermes Agent itself: the upstream checkout, its virtualenv, the
# service identity, and the skills this deployment actually calls.
#
# Separate from install-services.sh on purpose, and it must run *first*.
# install-services.sh writes configuration and unit files for a Hermes that
# this script is what puts on disk; without it, hermes-api.service has no
# interpreter to exec and systemd reports the failure as a restart loop rather
# than as a missing install.
#
# Idempotent. A second run re-uses an existing checkout and virtualenv, and
# never overwrites config.yaml, SOUL.md or /etc/hermes-agent/hermes.env.
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "run as root" >&2
  exit 1
fi

repository_root=${1:-$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)}
test -f "$repository_root/hermes/config.example.yaml"
test -f "$repository_root/systemd/hermes.env.example"

# Pinned to a commit, not a branch. Hermes is a fast-moving upstream: the
# provider-detection behaviour that hermes/config.example.yaml works around,
# the `gateway` entrypoint the unit execs, and the skills layout below are all
# properties of this specific revision. Override HERMES_COMMIT to move it, then
# re-run the verification block at the end of this script before trusting it.
HERMES_REPO=${HERMES_REPO:-https://github.com/NousResearch/hermes-agent.git}
HERMES_COMMIT=${HERMES_COMMIT:-d1afa16053a3777849c2b5465d59a0147b2172f9}

# Alternate prefixes exist so this script can be rehearsed end to end without
# touching a live robot. See docs/design/replication-guide.md.
HERMES_PREFIX=${HERMES_PREFIX:-/opt/hermes-agent}
HERMES_STATE=${HERMES_STATE:-/var/lib/hermes-agent}
HERMES_ETC=${HERMES_ETC:-/etc/hermes-agent}
HERMES_USER=${HERMES_USER:-hermes-agent}
HERMES_GROUP=${HERMES_GROUP:-hermes-agent}

fail() {
  echo "install-hermes: $1" >&2
  exit 1
}

# --- preconditions -----------------------------------------------------------
# Fail here, with an instruction, rather than three minutes into a pip build.

if command -v apt-get >/dev/null 2>&1; then
  # Done before the interpreter check, not after: a freshly imaged Raspberry Pi
  # OS Lite has no git and no python3 at all, and "install python yourself"
  # is a poor first instruction for a script whose whole job is to install
  # things. Several of Hermes' 25 dependencies also build from source on arm64
  # because no wheel is published, and missing headers surface as a compiler
  # error buried in pip output rather than as a clear message.
  echo "installing build prerequisites..."
  apt-get update
  apt-get install -y --no-install-recommends \
    ca-certificates git build-essential pkg-config \
    python3 python3-venv python3-dev libffi-dev libssl-dev
else
  echo "warning: no apt-get; ensure git, a C toolchain, libffi and libssl headers are present" >&2
fi

if ! command -v git >/dev/null 2>&1; then
  fail "git is required and could not be installed automatically.
  Install it and re-run: apt-get install -y git"
fi

# requires-python is >=3.11,<3.14 at the pinned commit. Prefer the oldest
# supported interpreter, which is what the reference deployment runs, and fall
# back to the distribution's default python3 when no versioned binary exists.
hermes_python=
for candidate in python3.11 python3.12 python3.13 python3; do
  command -v "$candidate" >/dev/null 2>&1 || continue
  if "$candidate" -c 'import sys; sys.exit(0 if (3,11) <= sys.version_info < (3,14) else 1)' \
    >/dev/null 2>&1; then
    hermes_python=$(command -v "$candidate")
    break
  fi
done
if [ -z "$hermes_python" ]; then
  fail "no supported interpreter found. Hermes needs Python >=3.11,<3.14.
  On Raspberry Pi OS (bookworm): apt-get install -y python3.11 python3.11-venv python3.11-dev"
fi

if ! "$hermes_python" -c 'import venv' >/dev/null 2>&1; then
  fail "$hermes_python cannot create virtualenvs. Install the venv module:
  apt-get install -y $(basename "$hermes_python")-venv"
fi
echo "using $hermes_python ($("$hermes_python" -V 2>&1))"

# --- identity ----------------------------------------------------------------
# install-services.sh creates the same user; both are idempotent so whichever
# runs first wins and the other is a no-op.

if ! getent group "$HERMES_GROUP" >/dev/null 2>&1; then
  groupadd --system "$HERMES_GROUP"
fi
if ! id "$HERMES_USER" >/dev/null 2>&1; then
  useradd --system --gid "$HERMES_GROUP" --home-dir "$HERMES_STATE" \
    --shell /usr/sbin/nologin "$HERMES_USER"
fi

install -d -o root -g root -m 0755 "$HERMES_PREFIX"
install -d -o "$HERMES_USER" -g "$HERMES_GROUP" -m 0700 "$HERMES_STATE"
install -d -o "$HERMES_USER" -g "$HERMES_GROUP" -m 0700 "$HERMES_STATE/.hermes"
install -d -o root -g "$HERMES_GROUP" -m 0750 "$HERMES_ETC"

# --- checkout ----------------------------------------------------------------

if [ -d "$HERMES_PREFIX/.git" ]; then
  current=$(git -C "$HERMES_PREFIX" rev-parse HEAD)
  if [ "$current" = "$HERMES_COMMIT" ]; then
    echo "hermes-agent already at $HERMES_COMMIT; skipping fetch."
  else
    echo "moving hermes-agent from $current to $HERMES_COMMIT..."
    git -C "$HERMES_PREFIX" fetch --quiet origin "$HERMES_COMMIT" \
      || git -C "$HERMES_PREFIX" fetch --quiet origin
    git -C "$HERMES_PREFIX" checkout --quiet --detach "$HERMES_COMMIT"
  fi
else
  # $HERMES_PREFIX may already exist and be non-empty: install-services.sh
  # creates $HERMES_PREFIX/fast-routes, and on a re-run after a partial install
  # there may be other debris. git refuses to clone into that, so clone beside
  # it and move the repository in.
  echo "cloning $HERMES_REPO at $HERMES_COMMIT..."
  staging=$(mktemp -d)
  # A blobless clone fetches full history cheaply, which is what lets us check
  # out an arbitrary pinned commit without downloading every blob ever written.
  git clone --quiet --filter=blob:none --no-checkout "$HERMES_REPO" "$staging/hermes"
  git -C "$staging/hermes" checkout --quiet --detach "$HERMES_COMMIT"
  # Move the working tree in with `tar -k`, which refuses to replace a file
  # that already exists rather than overwriting it. Existing directories are
  # merged, which is what makes fast-routes/ harmless; an existing *file*
  # stops the install. This script runs as root and is documented as safe to
  # re-run, so it must not silently overwrite something a person edited.
  if ! (cd "$staging/hermes" && tar -cf - .) | (cd "$HERMES_PREFIX" && tar -xkf -); then
    rm -rf "$staging"
    fail "$HERMES_PREFIX already contains a file that the Hermes checkout would
  replace, and this script will not overwrite it. Inspect the directory, move
  what you want to keep, and re-run."
  fi
  rm -rf "$staging"
fi

test -f "$HERMES_PREFIX/pyproject.toml" \
  || fail "checkout at $HERMES_PREFIX has no pyproject.toml; remove it and re-run"

# --- virtualenv --------------------------------------------------------------

if [ ! -x "$HERMES_PREFIX/.venv/bin/python" ]; then
  echo "creating virtualenv with $hermes_python..."
  "$hermes_python" -m venv "$HERMES_PREFIX/.venv"
fi

# Editable, matching the reference deployment: `git -C /opt/hermes-agent log`
# then describes exactly the code that is running, which is the only way to
# answer "what version is this robot?" during an incident.
echo "installing hermes-agent (editable); this takes several minutes on a Pi..."
"$HERMES_PREFIX/.venv/bin/pip" install --disable-pip-version-check --upgrade pip
"$HERMES_PREFIX/.venv/bin/pip" install --disable-pip-version-check -e "$HERMES_PREFIX"

# --- configuration -----------------------------------------------------------
# Never overwrite: a running Pi carries local tuning in both of these.

if [ ! -e "$HERMES_STATE/.hermes/config.yaml" ]; then
  install -o "$HERMES_USER" -g "$HERMES_GROUP" -m 0600 \
    "$repository_root/hermes/config.example.yaml" \
    "$HERMES_STATE/.hermes/config.yaml"
  echo "installed config.yaml"
else
  echo "config.yaml already present; leaving it alone."
fi

if [ ! -e "$HERMES_STATE/.hermes/SOUL.md" ]; then
  install -o "$HERMES_USER" -g "$HERMES_GROUP" -m 0600 \
    "$repository_root/hermes/SOUL.example.md" \
    "$HERMES_STATE/.hermes/SOUL.md"
  echo "installed SOUL.md"
else
  echo "SOUL.md already present; leaving it alone."
fi

if [ ! -e "$HERMES_ETC/hermes.env" ]; then
  install -o root -g "$HERMES_GROUP" -m 0640 \
    "$repository_root/systemd/hermes.env.example" "$HERMES_ETC/hermes.env"
  echo "installed hermes.env from the example; it still holds placeholders."
else
  echo "hermes.env already present; leaving it alone."
fi

# --- skills ------------------------------------------------------------------
# Hermes ships ~180 bundled skills. A speech robot that must answer in about a
# second cannot afford that much skill metadata in every prompt, so the bundle
# is switched off wholesale and the eight this deployment calls are installed
# explicitly.

install -d -o "$HERMES_USER" -g "$HERMES_GROUP" -m 0700 "$HERMES_STATE/.hermes/skills"
: > "$HERMES_STATE/.hermes/.no-bundled-skills"
chown "$HERMES_USER:$HERMES_GROUP" "$HERMES_STATE/.hermes/.no-bundled-skills"
chmod 0644 "$HERMES_STATE/.hermes/.no-bundled-skills"

install_skill_tree() {
  # $1 source directory, $2 destination directory
  source_directory=$1
  destination=$2
  test -d "$source_directory" || fail "skill source missing: $source_directory"
  install -d -o "$HERMES_USER" -g "$HERMES_GROUP" -m 0755 "$destination"
  (cd "$source_directory" && find . -type d) | while read -r relative; do
    install -d -o "$HERMES_USER" -g "$HERMES_GROUP" -m 0755 "$destination/$relative"
  done
  (cd "$source_directory" && find . -type f) | while read -r relative; do
    # 0644, owned by hermes-agent. What actually requires this is the
    # *directory*: weather.py keeps its location cache in its own scripts/
    # directory, so the service user must be able to create and rename a file
    # there. Owning the files as well is a consequence, not a requirement, and
    # it does leave the service able to rewrite its own skill code — these are
    # not read-only. Making the files root-owned would close that, but it is
    # not done here: the three upstream skills are copied in by the same
    # function, their write behaviour is not ours to assume, and the change
    # cannot be verified without a Pi. Recorded as a replication gap instead of
    # being guessed at.
    install -o "$HERMES_USER" -g "$HERMES_GROUP" -m 0644 \
      "$source_directory/$relative" "$destination/$relative"
  done
}

# Written for this project. They exist nowhere upstream: Japanese-language
# output shaped for speech, Japanese postal-code lookup, NHK headlines, Reiwa
# era years, tsubo. If these are lost, they are lost.
for skill in datetime email news units weather; do
  install_skill_tree "$repository_root/hermes/skills/$skill" \
    "$HERMES_STATE/.hermes/skills/$skill"
done

# Third-party skills, taken byte-for-byte from the pinned upstream checkout
# rather than vendored into this repository: upstream is their home, and the
# commit pin already makes the copy reproducible.
install_skill_tree "$HERMES_PREFIX/skills/productivity/maps" \
  "$HERMES_STATE/.hermes/skills/maps"
install_skill_tree "$HERMES_PREFIX/optional-skills/finance/stocks" \
  "$HERMES_STATE/.hermes/skills/finance/stocks"
install_skill_tree "$HERMES_PREFIX/optional-skills/health/fitness-nutrition" \
  "$HERMES_STATE/.hermes/skills/health/fitness-nutrition"

chown -R "$HERMES_USER:$HERMES_GROUP" "$HERMES_STATE/.hermes/skills"

# --- verification ------------------------------------------------------------
# The embeddings installer shipped without this and its first failure showed up
# as a systemd restart loop. Everything below is something hermes-api.service
# depends on at exec time; check it now, while the operator is still watching.

echo
echo "verifying..."

test -x "$HERMES_PREFIX/.venv/bin/python" \
  || fail "no interpreter at $HERMES_PREFIX/.venv/bin/python"

installed_python=$("$HERMES_PREFIX/.venv/bin/python" -V 2>&1) \
  || fail "$HERMES_PREFIX/.venv/bin/python will not run"
echo "  interpreter: $installed_python"

# This is the exact module the unit's ExecStart invokes. Importing it proves
# the editable install resolved and that all 25 dependencies are present.
"$HERMES_PREFIX/.venv/bin/python" -m hermes_cli.main --help >/dev/null 2>&1 \
  || fail "'python -m hermes_cli.main --help' failed. The editable install is
  incomplete; re-run this script and read the pip output."
echo "  hermes_cli.main responds to --help"

hermes_version=$("$HERMES_PREFIX/.venv/bin/pip" show hermes-agent 2>/dev/null \
  | sed -n 's/^Version: //p')
echo "  hermes-agent version: ${hermes_version:-unknown}"
echo "  checkout: $(git -C "$HERMES_PREFIX" rev-parse --short HEAD)"

# Every skill the deployment expects, checked by the path Hermes will use.
missing_skills=
for skill in datetime email news units weather maps finance/stocks health/fitness-nutrition; do
  [ -f "$HERMES_STATE/.hermes/skills/$skill/SKILL.md" ] \
    || missing_skills="$missing_skills $skill"
done
[ -z "$missing_skills" ] || fail "skills missing after install:$missing_skills"
echo "  8 skills present under $HERMES_STATE/.hermes/skills"

# weather.py is the one skill script with a non-obvious runtime contract: Hermes
# tool calls depend on it, and a partial install can leave it silently missing.
# Verify it exists and can run under the Hermes interpreter.
test -f "$HERMES_STATE/.hermes/skills/weather/scripts/weather.py" \
  || fail "weather skill script missing"
"$HERMES_PREFIX/.venv/bin/python" \
  "$HERMES_STATE/.hermes/skills/weather/scripts/weather.py" --help >/dev/null 2>&1 \
  || fail "weather skill script will not run under the Hermes interpreter"
echo "  weather skill script runs"

test -f "$HERMES_STATE/.hermes/.no-bundled-skills" \
  || fail "bundled-skill marker missing; Hermes would load ~180 extra skills"
test -f "$HERMES_STATE/.hermes/config.yaml" || fail "config.yaml missing"

# The single most expensive misconfiguration in this deployment's history.
grep -q 'provider: openai-api' "$HERMES_STATE/.hermes/config.yaml" || cat >&2 <<'PROVIDER'
warning: the installed config.yaml does not pin `model.provider: openai-api`.
Hermes will read OPENAI_API_KEY as an OpenRouter key and every request will
fail 401 against api.openai.com. Compare it with hermes/config.example.yaml.
PROVIDER

echo
cat <<NEXT
Hermes is installed. Next:

  sudo scripts/install-services.sh      # unit files, bridge, fast routes
  sudoedit $HERMES_ETC/hermes.env       # replace the placeholders
  sudo systemctl enable --now hermes-api.service
  curl -s 127.0.0.1:8642/health

hermes.env still needs, at minimum, a real OPENAI_API_KEY and an API_SERVER_KEY
you choose. See docs/design/replication-guide.md for what each key is for and
which ones are optional.
NEXT
