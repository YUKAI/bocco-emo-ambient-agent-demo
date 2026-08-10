# Rebuilding the BOCCO emo ambient-agent bridge

## Scope and reproducibility status

This guide covers the complete path from a blank SD card to the two-service
runtime:

```text
BOCCO emo / official app
  -> BOCCO Platform webhook
  -> Cloudflare Quick Tunnel
  -> bocco-bridge on 127.0.0.1:8787
  -> Hermes API on 127.0.0.1:8642
  -> OpenAI
  -> BOCCO Platform API
  -> BOCCO emo
```

The image scripts, bridge installer, systemd units, and local test commands in
this repository were inspected for this guide. The bridge has also run on a
Raspberry Pi 5 with Hermes Agent 0.19.1 and cloudflared 2026.7.3.

The Hermes installation (step 6) is now scripted and pinned, and
`scripts/install-hermes.sh` has been run end to end twice on `debian:bookworm`
arm64 — the same Debian release and architecture as Raspberry Pi OS Lite on a
Pi 5 — starting from an image with neither `git` nor Python installed. A clean
image build and flash were **not** repeated while writing this document, and the
BOCCO emo hardware itself cannot be rehearsed in a container. Those steps are
marked where the repository does not provide a fully pinned implementation.

Before integrating behavior, read [BOCCO emo Platform API: measured
behavior](bocco-api-findings.md). In particular, an HTTP success is not proof of
robot playback, API-posted messages echo under the human account identity, and
BOCCO refresh tokens rotate.

## What you need

### Hardware

- Raspberry Pi 5. The supplied image configuration is 64-bit ARM and explicitly
  targets Pi 5.
- A suitable Pi 5 power supply.
- A microSD card of at least 32 GB and a reader.
- A BOCCO emo registered to an account you control.
- A macOS build machine with Docker Desktop. Apple Silicon is the tested build
  path; an amd64 host additionally needs QEMU/binfmt support.
- Ethernet or 2.4/5 GHz Wi-Fi with outbound Internet access.

### Accounts and credentials

- A BOCCO Platform API access token, refresh token, and room UUID.
- An OpenAI API key with access to the configured model.
- No Cloudflare account is required for a Quick Tunnel. Quick Tunnels are meant
  for testing and have no availability guarantee; use a named tunnel and stable
  domain for a permanent installation.

The repository does not document how a new user obtains BOCCO Platform API
credentials. That account-enrollment step is therefore **unverified** here. Do
not scrape credentials from the official application or share another user's
token.

`API_SERVER_KEY` looks like a fifth credential but is not obtained from anywhere.
You invent it (`openssl rand -hex 32`) and write the *same* value into both
`/etc/hermes-agent/hermes.env` and `/etc/bocco-bridge/bridge.env`. A mismatch is
the most common first-boot failure: Hermes starts cleanly and every bridge
request gets a 401 that looks like an OpenAI problem and is not.

Two credentials are optional, and the deployment runs without them:

| Optional | Effect if absent |
|---|---|
| `ALPHA_VANTAGE_KEY` | The `finance` skill is installed but inert. |
| `GMAIL_ADDRESS` + `GMAIL_APP_PASSWORD` | The `email` skill answers `メール連携は未設定です。` and exits 0. |

## Repository layout used by the commands

The commands below assume this shape:

```text
<project>/                     # a checkout of this repository
  .env.example
  .env                         # local secrets; never commit
  bridge/  hermes/  systemd/  scripts/  docs/
  raspberry-img/
    README.md
    scripts/
```

Run every command from `<project>`, the repository root, unless a section says
otherwise. Replace all angle-bracket placeholders yourself; do not paste the
brackets literally.

For a public release, use a reviewed tag or full commit SHA and record it in the
deployment notes. This checkout does not currently supply one immutable release
identifier that covers both the image and bridge, so selecting the public source
revision remains a release-engineering step.

## 1. Prepare the image configuration

Create the only workstation secret file from the template:

```sh
cd <project>
test -e .env || cp .env.example .env
chmod 0600 .env
${EDITOR:-vim} .env
```

At minimum, configure:

| Variable | Purpose |
|---|---|
| `PI_HOSTNAME` | Pi mDNS name, without `.local` |
| `PI_PASSWORD` | Initial `pi` password; the renderer requires at least 12 characters |
| `WIFI_SSID`, `WIFI_PASSWORD` | Optional; set both or neither |
| `OPENAI_API_KEY` | Initial OpenAI secret |
| `BOCCO_REFRESH_TOKEN` | Initial BOCCO refresh token |
| `BOCCO_ACCESS_TOKEN` | Initial access token; optional to the image renderer, but required by the current bridge initializer when no token file exists |
| `BOCCO_ROOM_UUID` | Strongly recommended for a deterministic single-room setup |

`render_config.py` rejects a missing OpenAI key or refresh token and rejects a
partially specified Wi-Fi pair. Check configuration without building:

```sh
python3 raspberry-img/scripts/render_config.py --check
```

There is an important boundary mismatch: the image README/renderer permits an
empty access token and suggests starting from a refresh token, while the current
bridge integration requires both `BOCCO_ACCESS_TOKEN` and
`BOCCO_REFRESH_TOKEN` when it creates a missing `oauth.json`. For a new bridge,
provide a valid access-token pair or seed `oauth.json` through a separately
verified OAuth flow before first start. A refresh-token-only first start is not
substantiated by the current bridge code.

The generated cloud-init files and resulting customized image contain plaintext
secrets. They are ignored by Git but must still be treated as credentials.

## 2. Build the Pi image

Start Docker Desktop, then run:

```sh
cd <project>
bash raspberry-img/scripts/customize-and-build.sh
```

The wrapper validates `.env`, builds the OS, renders cloud-init, and injects it
into:

```text
raspberry-img/deploy/ambient-agent-pi.img
```

If a known-good base image already exists and only injected configuration has
changed:

```sh
bash raspberry-img/scripts/customize-and-build.sh --reuse-image
```

The current image build uses `rpi-image-gen` v2.1.0 on Debian Bookworm, creates a
64-bit Pi 5 image, enables SSH/cloud-init/mDNS, configures Japanese locale and
`Asia/Tokyo`, and installs Python 3, `venv`, `pip`, Git, curl, and related base
packages. It also sets the Wi-Fi regulatory domain to Japan. It does **not**
install Hermes Agent, cloudflared, or the BOCCO bridge.

This clean image build was not rerun for this documentation pass. The commands
and outputs above are verified by script inspection, not by a fresh SD-card
cycle.

## 3. Flash the SD card

From the image directory, detect the newly inserted disk:

```sh
cd <project>/raspberry-img
bash scripts/detect-sd.sh
```

Then substitute the exact disk identifier you inspected:

```sh
diskutil unmountDisk /dev/diskN
sudo dd if=deploy/ambient-agent-pi.img of=/dev/rdiskN bs=4M status=progress
diskutil eject /dev/diskN
```

`dd` is destructive. Resolve `/dev/diskN` from `diskutil list` and check its size
and removable-media identity before running it. Never guess the target.

## 4. Complete first boot and remove provisioning residue

Boot with network connectivity and wait for cloud-init. Connect using the
hostname you configured:

```sh
ssh pi@<pi-host>.local
cloud-init status --wait
uname -m
timedatectl
/usr/sbin/iw reg get
```

Expected architecture is `aarch64`. The image defaults to `Asia/Tokyo`; change
it if the robot's registered timezone differs:

```sh
sudo timedatectl set-timezone <IANA-timezone>
date
```

All schedule and greeting decisions use Pi-local time. A correct clock but wrong
timezone is still a functional error.

The first boot writes runtime credentials to `/home/pi/.env` with mode `0600`.
That file is only a provisioning source; the systemd services installed later do
not automatically read it. Move the required values into the service-owned
configuration as described below without printing them.

After first boot has completed successfully and the required values have been
transferred, remove secret-bearing provisioning artifacts:

```sh
sudo rm /boot/firmware/user-data
```

On the workstation, securely remove or otherwise protect generated cloud-init
files and the customized `.img`. Do not share or back up the injected image. A
new build from the source template is safer than retaining a credential-bearing
image.

For longer-lived hardware, install an SSH public key and disable password SSH
after confirming key access. The repository creates the initial password login
but does not automate this hardening.

If mDNS does not resolve, inspect the router's DHCP leases or ARP table instead
of assuming the Pi failed to boot. For 5 GHz failures, verify `country JP` and
inspect `no IR` channel flags with `/usr/sbin/iw`; `/usr/sbin` is not necessarily
in the `pi` user's default `PATH`.

## 5. Install cloudflared

The live development deployment used cloudflared 2026.7.3. The repository's
README uses the latest ARM64 `.deb`, which is convenient but not reproducible.
For repeatable work, download an explicitly reviewed ARM64 release, verify its
published checksum, and install it:

```sh
curl -fLO <cloudflared-arm64-deb-url>
sudo dpkg -i <downloaded-cloudflared-package.deb>
/usr/bin/cloudflared --version
```

The bridge owns cloudflared as a child process. Do not create a separate Quick
Tunnel service: the random URL changes on each launch, and the bridge must read
that exact child-process URL before registering `/webhooks/bocco`.

## 6. Install Hermes Agent

```sh
cd /home/pi/bocco-runtime      # or wherever this repository is checked out
sudo scripts/install-hermes.sh "$PWD"
```

About 15 minutes on a Pi 5, most of it `pip`. This must happen **before**
`install-services.sh` (step 8); the ordering is enforced rather than merely
documented, and the reason is in that step.

### What the installer does

Hermes is a public repository but is not published to PyPI, so it is installed
from a **pinned commit** — `d1afa16053`, which reports version 0.19.1 — rather
than from a branch or a package index. The pin is not caution for its own sake.
Three things this deployment depends on are properties of that specific revision:
the `gateway` entrypoint the systemd unit execs, the provider auto-detection the
configuration works around, and the paths of the three upstream skills the
installer copies.

The install is editable (`pip install -e`) into `/opt/hermes-agent/.venv`, so
`git -C /opt/hermes-agent log -1` answers "what is this robot running?" during an
incident. The service runs as a system user `hermes-agent` with
`/var/lib/hermes-agent` (mode `0700`) as `HOME`.

The editable install resolves Hermes' own dependency set, which includes the
`aiohttp` the API adapter needs. Installing Hermes core without its optional
dependencies produces a gateway that starts but provides no API server; that
failure mode is what this step exists to prevent.

Re-running is safe. An existing checkout is fetched to the pin rather than
re-cloned, an existing virtualenv is re-used, and `config.yaml`, `SOUL.md` and
`hermes.env` are never overwritten — a running Pi carries local tuning in all
three.

### Skills

Hermes bundles roughly 180 skills. A robot that must answer aloud in about a
second cannot carry that much skill metadata in every prompt, so the bundle is
switched off wholesale with an empty marker file,
`/var/lib/hermes-agent/.hermes/.no-bundled-skills`, and eight skills are
installed explicitly instead.

Five of them were written for this project and exist nowhere upstream:

| Skill | What it does that no upstream skill does |
|---|---|
| `weather` | Japanese postal-code lookup via Zipcloud before Open-Meteo geocoding; one spoken Japanese sentence; a `--warm-cache` prefetch path |
| `datetime` | Reiwa era years and Japanese weekday names |
| `units` | Japanese household units — 坪 (tsubo) alongside metric and US customary |
| `news` | NHK's official Japanese RSS feed, titles only |
| `email` | Read-only Gmail IMAP shaped for the morning briefing; degrades silently when unconfigured |

These live in `hermes/skills/` in this repository and are installed from there.
They are stdlib-only Python 3.11 with no pip dependencies, which is what makes
them safe to run from the Hermes virtualenv.

The other three — `maps`, `finance/stocks`, `health/fitness-nutrition` — are
third-party skills taken byte-for-byte from the pinned upstream checkout at
`skills/productivity/maps`, `optional-skills/finance/stocks` and
`optional-skills/health/fitness-nutrition`. They are deliberately not vendored
here: upstream is their home, and the commit pin already makes the copy
reproducible. If upstream moves them, the installer's verification fails loudly
rather than quietly installing five skills and reporting success.

`SOUL.md` — the standing instruction that tells the agent to *call* skills rather
than answer weather and news questions from model memory — is installed from
`hermes/SOUL.example.md`. Without it the agent will happily invent a forecast.

### Verification the installer performs

Before claiming success it checks every dependency `hermes-api.service` has at
exec time:

- the virtualenv interpreter exists and runs;
- `python -m hermes_cli.main --help` succeeds, which proves the editable install
  resolved and all of Hermes' dependencies are present;
- all eight `SKILL.md` files are where Hermes will look for them;
- `weather.py` runs under the Hermes interpreter — it is the script both the
  bridge's fast route and the morning briefing shell out to;
- `.no-bundled-skills` and `config.yaml` exist, and the config still pins the
  provider.

This is deliberate. `install-embeddings.sh` shipped without a post-install check
and its first failure surfaced as a systemd restart loop instead of a readable
error.

### Rehearsing without a Pi

Every path in `install-hermes.sh` is overridable, so the whole install can be run
against throwaway prefixes:

```sh
docker run --rm --platform linux/arm64 -v "$PWD":/repo:ro debian:bookworm \
  sh -c 'sh /repo/scripts/install-hermes.sh /repo'
```

or, on a machine you do not want to disturb:

```sh
sudo HERMES_PREFIX=/tmp/hermes-test/opt \
     HERMES_STATE=/tmp/hermes-test/var \
     HERMES_ETC=/tmp/hermes-test/etc \
     scripts/install-hermes.sh "$PWD"
```

`HERMES_REPO` and `HERMES_COMMIT` are overridable the same way. If you move the
pin, re-run the verification block before trusting it.

## 7. Test and transfer a pinned bridge revision

On the workstation, use a disposable test venv:

```sh
cd <project>
python3 -m venv .venv-test
. .venv-test/bin/activate
pip install -e ./bridge
scripts/test-bridge.sh
deactivate
```

The suite uses fake services and does not need live BOCCO or OpenAI credentials.
Record the exact tested commit. To avoid copying another developer's uncommitted
files, export that commit to a clean staging directory and sync the export:

```sh
cd <project>
git rev-parse --verify '<full-commit-sha>^{commit}'
stage_dir=$(mktemp -d)
git archive --format=tar <full-commit-sha> | tar -xf - -C "$stage_dir"
rsync -az --delete --exclude '.git' --exclude '.venv*' \
  --exclude '__pycache__' "$stage_dir/" pi@<pi-host>:/home/pi/bocco-runtime/
```

Remove the staging directory after checking that it is the exact export you
intended. Never apply `--delete` to `/home/pi`, `/var/lib`, or another broad
target; only the disposable runtime source tree is safe to mirror this way.

## 8. Run the service installer

On the Pi:

```sh
cd /home/pi/bocco-runtime
sudo sh scripts/install-services.sh "$PWD"
```

This step refuses to run while `/opt/hermes-agent/.venv/bin/python` is absent,
because it installs and enables `hermes-api.service`, whose `ExecStart` is that
interpreter. Enabling a unit whose interpreter does not exist produces
`status=203/EXEC` and a three-second restart loop — a symptom that reads like a
crashing service rather than a skipped step. Run step 6 first.

The installer:

- creates unprivileged `bocco-bridge` and `hermes-agent` users;
- installs the bridge into `/opt/bocco-bridge/.venv`;
- creates private state directories;
- installs loopback-only, hardened systemd units;
- installs root-owned weather, date/time, and news fast-route scripts under
  `/usr/local/share/bocco-bridge/fast-skills/`; and
- creates secret-free environment/configuration templates only when the target
  file does not already exist.

Rerunning the installer upgrades bridge code and shared fast-route files, but
does not replace existing service environment files or the existing Hermes
configuration.

Confirm the fast-route files can be read by the bridge:

```sh
sudo -u bocco-bridge test -r /usr/local/share/bocco-bridge/fast-skills/weather.py
sudo -u bocco-bridge test -r /usr/local/share/bocco-bridge/fast-skills/time_info.py
sudo -u bocco-bridge test -r /usr/local/share/bocco-bridge/fast-skills/news.py
```

Do not enable either service while placeholder values remain.

## 9. Configure service secrets without exposing them

The installed files and expected permissions are:

| File | Owner/group | Mode |
|---|---|---:|
| `/etc/bocco-bridge/bridge.env` | `root:bocco-bridge` | `0640` |
| `/etc/hermes-agent/hermes.env` | `root:hermes-agent` | `0640` |
| `/var/lib/hermes-agent/.hermes/config.yaml` | `hermes-agent:hermes-agent` | `0600` |
| `/var/lib/bocco-bridge/oauth.json` | `bocco-bridge:bocco-bridge` | `0600` |

Use `sudoedit`, a root-only editor, or a script that reads source files and writes
the destinations without printing values. Never place a token in a shell command
argument, journal message, or Git-tracked file.

Generate one shared Hermes API key on the Pi and write the same value to
`API_SERVER_KEY` in both environment files. For example, use a root-only helper
that invokes `openssl rand -hex 32` internally and updates both files; do not echo
the generated result to the terminal.

### Bridge environment

Start from `systemd/bocco-bridge.env.example` and replace every placeholder. The
important inputs are:

- `BOCCO_WEBHOOK_SECRET`: a non-empty initial random value. After Quick Tunnel
  registration, the runtime replaces the in-memory verifier with the newly
  returned webhook secret.
- `BOCCO_PLATFORM_BASE_URL=https://platform-api.bocco.me`
- `BOCCO_ACCESS_TOKEN` and `BOCCO_REFRESH_TOKEN`: used only to initialize a
  missing `oauth.json`; once that file exists, its stored state wins. Both
  environment values are required for initialization in the current
  implementation.
- `BOCCO_TOKEN_FILE=/var/lib/bocco-bridge/oauth.json`
- `BOCCO_ROOM_UUID`: the target room. Set it. Beyond the one startup
  `voice_speed` fetch it is also what the Webhook delivery self-heal probes
  through; with it unset the bridge cannot tell a silent house from a severed
  delivery path.
- `HERMES_API_URL=http://127.0.0.1:8642`
- `API_SERVER_KEY`: identical to the Hermes environment value.
- `BOCCO_BRIDGE_DEPENDENCY_FACTORY=bocco_bridge.integration:create_dependencies`
- `BOCCO_BRIDGE_TUNNEL_ENABLED=true`
- `BOCCO_BRIDGE_CLOUDFLARED=/usr/bin/cloudflared`
- `BRIDGE_WEBHOOK_PROBE=on`: detects and repairs the quick tunnel's silent
  delivery stall. Deliberately on by default; the failure it covers is
  invisible, total, and has happened three times in one day on this
  deployment.
- `DEFAULT_LOCATION`: the default city used by the audited weather route.
- Optional `BRIDGE_ROBOT_NICKNAME`; optional `BRIDGE_PERSONA` may remain unset
  because it can be configured later through the supported in-chat command.

Leave `BOCCO_AGENT_USER_UUID` unset when API calls use a human's personal OAuth
token. Human and API messages then share the same sender UUID; setting the human
UUID as a bot UUID suppresses real input.

On a fresh state directory, the bridge seeds `oauth.json` from the environment
only when that token file does not exist. After any successful refresh, the
current access token and newly rotated refresh token live in `oauth.json` and are
atomically persisted. From then on, `oauth.json` is authoritative. Never restore
an older token from `.env`, the workstation, a previous image, or a stale backup;
doing so can lose account access after rotation.

After the service environment is complete and the authoritative token file has
been created or restored, remove the duplicate provisioning copy at
`/home/pi/.env`. Confirm the service does not depend on it first; neither shipped
systemd unit names that file.

If migrating an existing installation, copy the current authoritative
`oauth.json` before the first start and restore its exact ownership/mode. Do not
start with stale environment tokens and overwrite it later.

### Hermes environment

Start from `systemd/hermes.env.example`:

```text
OPENAI_API_KEY=<secret>
API_SERVER_KEY=<same secret as bridge.env>
API_SERVER_ENABLED=true
API_SERVER_HOST=127.0.0.1
API_SERVER_PORT=8642
```

The systemd unit reads this file through `EnvironmentFile=`. The OpenAI key
belongs only in the environment file, not in Hermes YAML.

### Hermes API-only configuration

`scripts/install-hermes.sh` installs `hermes/config.example.yaml` as
`/var/lib/hermes-agent/.hermes/config.yaml` (owner `hermes-agent`, mode `0600`)
and does not overwrite an existing one. Nothing here is a manual step any more,
but one block in it must survive local editing:

```yaml
model:
  provider: openai-api
  default: gpt-5.6-luna
  max_tokens: 1000
```

With no `model.provider`, Hermes infers the provider from the shape of the
environment and reads `OPENAI_API_KEY` as an **OpenRouter** key. Every request
then fails 401 against `api.openai.com`, and the failure surfaces as a generic
upstream error rather than as a configuration mistake. This cost the project a
full debugging session. The installer warns at the end of every run if the
installed config has lost the pin.

`OPENAI_API_KEY` is read from the service environment; do not copy its value into
YAML. Model availability is account- and time-dependent, so select another
Hermes-compatible OpenAI model if your account cannot use the default and record
the substitution.

The same file also disables Hermes' general-agent prompt boilerplate
(`tool_use_enforcement`, `task_completion_guidance`,
`parallel_tool_call_guidance`, `environment_probe`). That is deployment policy,
not taste: the persona arrives per-request from the bridge, and those blocks cost
roughly 4 KB of prompt on every turn of a system whose reply-latency budget is
2-4 seconds.

`compression.threshold_tokens` ships at 8000, a conservative value sized for a
Pi. The reference deployment later raised it to 40000 once conversations grew
long enough to compress mid-session. That is local tuning: edit the installed
`config.yaml`, not the example.

Keep the configuration API-only. Do not add Discord, Telegram, Slack, or other
gateway channels. Binding remains controlled by the environment's
`API_SERVER_HOST=127.0.0.1`.

Verify metadata without dumping file contents:

```sh
sudo chown root:bocco-bridge /etc/bocco-bridge/bridge.env
sudo chmod 0640 /etc/bocco-bridge/bridge.env
sudo chown root:hermes-agent /etc/hermes-agent/hermes.env
sudo chmod 0640 /etc/hermes-agent/hermes.env
sudo chown -R hermes-agent:hermes-agent /var/lib/hermes-agent/.hermes
sudo chmod 0700 /var/lib/hermes-agent
find /var/lib/hermes-agent ! -user hermes-agent -print
```

The final `find` should print nothing. Root ownership of the `/etc` files is
intentional because the services only read them; service state and logs under
`/var/lib` must be writable by the service users.

## 10. Start and verify Hermes first

```sh
sudo systemctl enable --now hermes-api.service
systemctl --no-pager --full status hermes-api.service
```

Watch it for at least 60 seconds and inspect its journal:

```sh
sleep 60
systemctl show hermes-api.service -p ActiveState -p SubState -p NRestarts
sudo journalctl -u hermes-api.service --since '-2 minutes' --no-pager
```

Expected results:

- `active/running` and no unexplained restart;
- no `aiohttp not installed` or `No adapter available for api_server` message;
- the API adapter starts on `127.0.0.1:8642`;
- the configured skills directory is discovered without a permission error.

An unauthenticated request may return 401 or 403; that proves the listener and
authentication boundary are present:

```sh
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8642/
```

Before connecting the robot, make one tiny authenticated generation request and
confirm it returns HTTP 200. Read `API_SERVER_KEY` inside a root-only or
Hermes-user script and put it in the HTTP header in memory. Do not pass it as a
command-line argument or print the request headers. A successful socket check
alone does not detect an invalid OpenAI provider/key.

## 11. Start and verify the bridge

```sh
sudo systemctl enable --now bocco-bridge.service
systemctl --no-pager --full status bocco-bridge.service
```

Readiness requires SQLite, the worker, Quick Tunnel establishment, and webhook
registration. Allow up to about 90 seconds:

```sh
curl --fail http://127.0.0.1:8787/healthz
for attempt in $(seq 1 18); do
  curl --fail http://127.0.0.1:8787/readyz && break
  sleep 5
done
```

Then run the repository check and inspect state without printing secrets:

```sh
cd /home/pi/bocco-runtime
scripts/check-bridge.sh
systemctl show bocco-bridge.service -p ActiveState -p SubState -p NRestarts
sudo journalctl -u bocco-bridge.service --since '-3 minutes' --no-pager
sudo stat -c '%U:%G %a %n' /var/lib/bocco-bridge/oauth.json
ss -ltn
```

Expected results:

- bridge and Hermes are `active/running`;
- bridge `/healthz` and `/readyz` return 200;
- the journal reports a tunnel URL was obtained and webhook operations
  succeeded, without needing to reproduce the full URL in deployment notes;
- the subscription contains at least `message.received`, `radar.detected`,
  `accel.detected`, `illuminance.changed`, `emo_talk.finished`,
  `motion.finished`, `recording.started`, and `recording.finished`;
- `webhook_probe_armed` appears once, followed within about half a minute by
  `webhook_probe_confirmed` — that is the bridge proving BOCCO is actually
  delivering to the hostname it just registered, which registration success
  alone does not establish (see "Everything is green and the robot answers
  nothing" below). `webhook_probe_disabled reason=no_room` instead means
  `BOCCO_ROOM_UUID` is unset and the check is not running;
- `oauth.json` is `bocco-bridge:bocco-bridge 600`;
- ports 8787 and 8642 listen only on loopback. SSH may listen on a network
  interface; neither application port should.

The bridge refreshes the motion catalog and starts its local scheduler during
startup. With no schedules defined, a scheduler tick should do no work. Private
databases are expected at:

```text
/var/lib/bocco-bridge/state.db
/var/lib/bocco-bridge/memory.db
```

They should be owned by `bocco-bridge` and not readable by other users.

## 12. Perform a human-supervised acceptance test

Do this only with someone beside the robot. Keep the first trial short:

1. Send one text message in the official app. Confirm one short robot reply and
   no reply-to-itself loop.
2. Speak one short phrase to the robot. Confirm the recording produces one
   response.
3. Verify the journal records outbound-ID echo suppression rather than handling
   the bridge's reply as human input.
4. Touch or lift the robot once and identify the actual `accel.detected` kind.
   Firmware classifications are noisy, and a real lift may look like a settle or
   put-down state.
5. Redact tokens, personal text, UUIDs, and the Quick Tunnel URL before retaining
   any fixture or log excerpt.

Do not use an arbitrary API send as a health probe: it creates a real room
message and may make the robot speak or move. Stamps and uploaded audio also add
chat history. See the findings document for their measured side effects.

## Common failure modes

### Hermes exits because it cannot write `agent.log`

Symptom: a journal traceback names a path under
`/var/lib/hermes-agent/.hermes/logs`; the directory is owned by root and mode
`0700`.

Cause: a previous root-run Hermes command created part of the state tree.

Repair:

```sh
sudo chown -R hermes-agent:hermes-agent /var/lib/hermes-agent/.hermes
sudo chmod 0700 /var/lib/hermes-agent
find /var/lib/hermes-agent ! -user hermes-agent -print
```

The `find` result must be empty before restarting once. Do not enter a blind
restart loop.

### Hermes says aiohttp is absent or no API adapter is available

This means the virtualenv was not built by `scripts/install-hermes.sh`, or the
editable install did not complete. Re-run the installer; it is idempotent and
will not touch `config.yaml`, `SOUL.md` or `hermes.env`. Installing aiohttp into
the system Python does not repair a service using the private venv.

### Hermes listens but generation returns provider 401

Check names, not values:

- `/etc/hermes-agent/hermes.env` contains `OPENAI_API_KEY`;
- `hermes-api.service` has `EnvironmentFile=/etc/hermes-agent/hermes.env`;
- YAML declares `model.provider: openai-api` and an available model;
- the key value is not a placeholder, expired, or unauthorized for that model.

An API-server 401 from an unauthenticated local curl is expected. A provider 401
after the request passes Hermes authentication is not.

### Bridge health is 200 but readiness never becomes 200

Check, in order:

1. `/usr/bin/cloudflared` exists and is executable.
2. The Pi has outbound DNS/HTTPS connectivity.
3. The child tunnel produced a current `trycloudflare.com` URL.
4. Webhook registration succeeded with current OAuth state.
5. `/var/lib/bocco-bridge/oauth.json` is writable and mode `0600`.

Do not copy an old Quick Tunnel URL from a previous journal boot. The URL is
random and the bridge deliberately registers the current child process.

### Everything is green and the robot answers nothing

This is the most disruptive recurring failure in this deployment, and the one
that is hardest to recognize, because every signal you would normally check says
the system is fine.

A Cloudflare **quick tunnel** mints a new random `trycloudflare.com` hostname on
every start, and the bridge registers that hostname with the Platform API each
time. The registration is usually honored. Sometimes it is accepted and then
never used: BOCCO stops delivering webhooks to the bridge entirely, and does not
resume.

What that looks like:

- `bocco-bridge.service` is `active/running`; `/healthz` and `/readyz` return
  200; cloudflared is up; the event queue is empty;
- the registered URL is publicly reachable and answers correctly — 401 to a
  wrong secret, 400 to a malformed body — verified from two different networks;
- **no error, warning, or any other line appears in the journal**;
- and no webhook arrives. A typed message and a spoken message that both must
  have produced `message.received` produced nothing at all.

Observed three times in one day on 2026-08-04, each time cleared by restarting
the bridge — which mints a *new* hostname and re-registers. Diagnosing it by
hand the first time took about twenty minutes, almost all of it spent proving
that the parts that were working were working.

**What the bridge now does about it.** Since a silent room and a severed
delivery path look identical from the inside, the bridge proves delivery is
alive rather than inferring it. It posts a custom motion document to
`POST /v1/rooms/{room}/motions`, which echoes back as a `message.received`
webhook; the document commands no movement (one head transition with every
control point null — "hold the posture you already have") and custom documents
carry no sound in the tested firmware, so the probe is silent and invisible. The
one unavoidable trace is a motion entry in the room's chat history in the
official app.

It probes five seconds after every registration — including the tunnel's own
restarts — and after six hours of total inbound silence, the latter only between
08:00 and 22:00 local so a quiet house is never disturbed at night. Any inbound
webhook arriving during the twenty-second wait counts as proof, so real traffic
regularly makes the probe unnecessary.

If nothing comes back it re-registers the same URL and probes again; if that
also fails it recycles the tunnel so a new hostname is minted and registered —
the same thing a manual restart accomplishes — and probes a third time. Then it
stops. Three attempts is the whole budget; there is no restart loop and no
sustained extra load on the Platform API.

**What you will see in the journal.** Nothing at all while delivery is healthy
except an occasional confirmation:

```text
webhook_probe_armed silence_s=21600 timeout_s=20 max_attempts=3
webhook_probe_started reason=registration attempt=1
webhook_probe_confirmed reason=registration attempt=1 elapsed_ms=2310
```

A stall that heals itself:

```text
webhook_probe_timeout reason=registration attempt=1 waited_s=20
webhook_delivery_stalled reason=registration failed_attempts=1 action=reregister
tunnel_reregistered provider=quick_tunnel
webhook_probe_started reason=registration attempt=2
webhook_delivery_recovered reason=registration attempts=2 action=reregister outage_s=48
```

A stall that needed a new hostname shows `action=recycle_tunnel` and
`tunnel_recycling` in place of `action=reregister`. A stall the bridge could not
fix ends at `ERROR` and then goes quiet:

```text
webhook_delivery_unrecoverable reason=silence attempts=3 outage_s=105 hint=restart_bocco_bridge_service
```

At that point restart the service by hand — the pre-existing behavior, now with
a line in the journal telling you to. If delivery returns on its own the bridge
says `webhook_delivery_restored source=inbound_traffic` and re-arms itself. No
line ever contains the tunnel URL or the Webhook secret.

**Configuration.** `BRIDGE_WEBHOOK_PROBE` is on by default but requires
`BOCCO_ROOM_UUID` and `BRIDGE_MOTIONS_ENABLED`; without a room to post into
there is nothing to probe with, and the journal says so once at startup
(`webhook_probe_disabled reason=no_room`). Set `BOCCO_ROOM_UUID` if you have not
already. Timing, attempt count and the waking-hours window are tunable — see the
commented block at the end of `systemd/bocco-bridge.env.example`. The attempt
count can only be lowered: three is the ceiling as well as the default, because
it counts rungs on a fixed ladder and there is no remedy after the recycle. A
larger value is rejected at startup rather than silently recycling the tunnel
over and over.

**Where it can still fail silently.** If the Platform API itself is unreachable
the probe cannot be sent, and the bridge logs `webhook_probe_send_failed` and
deliberately does *not* spend its repair budget on a diagnosis it never made. A
stall that begins at 22:01 in a house that then stays quiet is not detected
until 08:00 the next morning. And if the probe motion itself is what BOCCO stops
delivering — while other event types still arrive — the probe would report
healthy; nothing observed so far suggests per-media delivery, but it has not
been ruled out.

### A BOCCO refresh fails after a previous successful refresh

Assume the initial `.env` and workstation copies are stale. BOCCO rotates refresh
tokens. Preserve the newest atomically written
`/var/lib/bocco-bridge/oauth.json`; never reseed it from an older image or
environment file.

### The bridge replies to itself

Do not identify the bridge by sender UUID when using personal OAuth. Confirm the
Platform POST response's `unique_id` is recorded and consumed on the matching
`message.received` echo. Leave `BOCCO_AGENT_USER_UUID` unset unless a genuinely
separate posting identity exists.

### A message POST succeeded but the robot did not visibly react

HTTP 2xx and a returned ID mean API acceptance, not playback. In one retained
sample, one of 44 accepted outbound chunks had neither its expected echo nor a
nearby stock new-message motion. Do not retry automatically unless your product
can tolerate a delayed duplicate.

### A recording finishes but no conversation reply starts

The Platform can emit `recording.finished` without a subsequent genuine audio
`message.received`; this occurred once among 41 retained recording finishes. The
bridge has no text to transcribe or answer in that case. Correlate with local
arrival time because BOCCO timestamps can be reordered.

### Motion completion never arrives

This is expected for custom motion documents in the measured firmware. API-sent
preset identities also did not appear in the retained `motion.finished` census.
Do not block the event worker waiting for a callback that may never arrive.

### Touch speech sometimes arrives many seconds late

The single worker is not preemptive. A long background reaction-bank refresh was
observed delaying one touch reaction by 10.875 seconds. Ensure background
generation is not awaited inside the sole event worker in the revision you
deploy. Normal cloud touch-to-speech latency is still roughly 1.9-2.7 seconds;
the firmware's immediate non-verbal reflex is the only first-second response.

### Fast weather/time/news falls back to the model

Rerun `install-services.sh` and verify the three shared files are readable by
`bocco-bridge`. A missing, failed, slow, or empty fast-route script intentionally
falls through to Hermes. Weather/news also require outbound network access;
date/time is local.

### Schedules or greetings use the wrong time of day

Compare `timedatectl` with the timezone registered for the robot. The image's
`Asia/Tokyo` default is not correct for every deployment.

### Application ports are exposed on the LAN

Stop the services and correct `API_SERVER_HOST` and the unit/configuration before
continuing. Expected listeners are `127.0.0.1:8642` and `127.0.0.1:8787`; the
Quick Tunnel is an outbound process. Do not use `0.0.0.0` as a convenience.

## Updating or rebuilding without losing identity

Before an upgrade, privately back up the current state files if policy permits:

- `/var/lib/bocco-bridge/oauth.json` — critical, authoritative rotated tokens;
- `/var/lib/bocco-bridge/state.db` — queue, delivery, settings, scheduler state;
- `/var/lib/bocco-bridge/memory.db` — explicit household memory;
- `/var/lib/hermes-agent/.hermes/` — Hermes configuration and local state.

Backups contain credentials and personal data. Encrypt them, restrict access,
and do not include them in source control or a public image.

For a bridge-only update, test and export a pinned commit, sync only
`/home/pi/bocco-runtime/`, rerun `install-services.sh`, and restart only
`bocco-bridge.service`. Existing `/etc` and `/var/lib` files are outside the
source tree and are preserved by the installer. Recheck ownership after any
root-run diagnostic.

For a full SD-card rebuild, seed the new machine with the latest authoritative
`oauth.json` **before the first bridge start**. The fallback BOCCO values injected
by the old image may already be invalid after token rotation.

## Known replication gaps

The following items could not be substantiated as fully reproducible from the
repository alone:

- BOCCO Platform developer enrollment and initial credential issuance are not
  documented, and the first BOCCO OAuth token pair is obtained interactively. The
  bridge maintains the pair afterwards in `/var/lib/bocco-bridge/oauth.json`.
- BOCCO Webhook registration is manual. The bridge serves the Webhook, but
  pointing BOCCO at your tunnel URL happens in the BOCCO developer console, and
  the URL changes every time a `cloudflared` quick tunnel restarts. A stable
  named tunnel avoids the re-registration and needs a Cloudflare account.
- The `email` skill's credentials are not wired by any installer. It is installed
  and inert until `GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD` are added to
  `hermes.env`. This is intentional — it is the only skill that reads personal
  data.
- Installed skill files are owned by `hermes-agent` and mode 0644, so the Hermes
  service can rewrite its own skill code. The directory has to be writable by
  that user — `weather.py` keeps its location cache beside itself — but the
  files do not, and making them root-owned would be the stronger posture. It is
  not done yet because the three upstream skills are installed by the same code
  path and their write behaviour is not ours to assume. Moving the weather cache
  out of the skills tree would remove the constraint entirely.
- Hermes is pinned to one upstream commit, so it does not receive upstream fixes
  until someone moves `HERMES_COMMIT` and re-runs the installer. That is the
  intended trade: an agent framework that changes under a robot is worse than an
  old one.
- The root image does not install cloudflared, Hermes, bridge services, or their
  final `/etc` configuration; all are post-boot steps.
- The image renderer allows a refresh-token-only configuration, but the current
  bridge initializer requires an access token as well when `oauth.json` is
  absent.
- The build scripts do not pin the cloudflared release used at install time.
- The source tree does not identify one public release/tag that fixes the image,
  bridge, Hermes, and cloudflared versions together.
- A clean image build, flash, first boot, and complete acceptance cycle were not
  rerun during this documentation pass.
- The quick tunnel's silent delivery stall has no known cause on the Platform
  side. The bridge now detects it and repairs it (see "Everything is green and
  the robot answers nothing"), but the remedy is empirical: re-registering and
  then replacing the hostname are what a manual restart happens to do, not a
  documented recovery procedure. A named tunnel on a domain you own would avoid
  the rotating hostname entirely; it is deliberately not required here, because
  most people replicating this do not own a domain.

The remaining gaps should become release artifacts rather than tribal knowledge:
a version manifest and a secret-free post-boot provisioning script would make the
process genuinely one-command reproducible. The verified Hermes installer and the
provider-complete YAML example that used to be on this list now exist.

## Repository references

- `raspberry-img/README.md`
- `raspberry-img/scripts/customize-and-build.sh`
- `raspberry-img/scripts/build.sh`
- `raspberry-img/scripts/render_config.py`
- `raspberry-img/scripts/inject-cloud-init.sh`
- `scripts/install-hermes.sh`
- `scripts/install-services.sh`
- `scripts/check-bridge.sh`
- `hermes/skills/`
- `hermes/SOUL.example.md`
- `systemd/bocco-bridge.service`
- `systemd/hermes-api.service`
- `systemd/*.env.example`
- `hermes/config.example.yaml`
- [Bridge runtime](bridge-runtime.md)
- [Bridge architecture](bridge-architecture.md)
