import imaplib
import importlib.util
import inspect
from pathlib import Path
import unittest


REPOSITORY = Path(__file__).resolve().parents[3]


def _text(relative: str) -> str:
    """Read a repository file as UTF-8, whatever the process locale is.

    `Path.read_text()` with no encoding uses the locale's preferred encoding.
    Several files these tests read contain Japanese, so under a POSIX/C locale
    — which is exactly what a CI runner may hand you — the read raises
    UnicodeDecodeError. Naming the encoding in one place is what stops that
    coming back one call site at a time.
    """

    return (REPOSITORY / relative).read_text(encoding="utf-8")

def _load_skill_module(relative_path: str, name: str):
    """Import a Hermes skill script by path.

    The skills live outside the bridge package because Hermes installs them
    from `hermes/skills/`, not from a Python distribution. They are stdlib-only
    by construction, so importing one here costs nothing and is the only way to
    execute the rescued code in CI.
    """
    path = REPOSITORY / relative_path
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        # Not an `assert`: `python -O` strips those, and the failure would then
        # surface as `module_from_spec(None)` several frames later. This test
        # exists to prove a rescued skill is present and importable, so the
        # error should name the file that was not.
        raise ImportError(f"cannot load a module specification from {path}")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


class DeploymentFileTests(unittest.TestCase):
    def test_bridge_unit_uses_dedicated_unprivileged_identity(self) -> None:
        unit = _text("systemd/bocco-bridge.service")
        self.assertIn("User=bocco-bridge", unit)
        self.assertIn("Group=bocco-bridge", unit)
        self.assertIn("EnvironmentFile=/etc/bocco-bridge/bridge.env", unit)
        self.assertIn("NoNewPrivileges=yes", unit)
        self.assertIn("ProtectSystem=strict", unit)
        self.assertIn("StateDirectoryMode=0700", unit)
        self.assertNotIn("0.0.0.0", unit)

    def test_both_units_pin_a_utf8_locale(self) -> None:
        # systemd hands a service the *system* default locale, not a login
        # one, and on a machine where locale configuration never ran that
        # default is C/POSIX. Python encodes subprocess arguments with the
        # filesystem encoding, which is then ASCII, so the weather fast route
        # raises UnicodeEncodeError while spawning weather.py with a place name
        # like 大阪 — before the script runs, with a traceback that points at
        # fork_exec rather than at anything recognisable.
        #
        # The Pi image configures a Japanese locale, which is why this was
        # never observed; nothing in this repository required it, so a plain
        # Debian install reproduced from these files would have had a weather
        # route that failed on every Japanese place name.
        #
        # C.UTF-8 is built into glibc on bookworm and needs no locale
        # generation, so pinning it costs nothing and works anywhere.
        for name in ("bocco-bridge.service", "hermes-api.service"):
            unit = _text(f"systemd/{name}")
            self.assertIn("Environment=LANG=C.UTF-8", unit, name)
            self.assertIn("Environment=LC_ALL=C.UTF-8", unit, name)

    def test_hermes_api_unit_uses_a_separate_identity(self) -> None:
        unit = _text("systemd/hermes-api.service")
        self.assertIn("User=hermes-agent", unit)
        self.assertIn("EnvironmentFile=/etc/hermes-agent/hermes.env", unit)
        self.assertNotIn("User=bocco-bridge", unit)

    def test_embedding_unit_is_niced_loopback_only_and_optional(self) -> None:
        unit = _text("systemd/bocco-embeddings.service")
        self.assertIn("User=bocco-embeddings", unit)
        self.assertIn("EnvironmentFile=/etc/bocco-embeddings/embeddings.env", unit)
        # The two lines that keep a model forward pass from ever competing with
        # the bridge's single event loop.
        self.assertIn("Nice=10", unit)
        self.assertIn("CPUWeight=20", unit)
        # Loopback only, and never a dependency of the bridge in either
        # direction: retrieval degrades to BM25 when this is not running, so an
        # ordering edge would claim a coupling that does not exist.
        self.assertIn("--host 127.0.0.1", unit)
        self.assertNotIn("0.0.0.0", unit)
        self.assertNotIn("bocco-bridge.service", unit)
        self.assertIn("NoNewPrivileges=yes", unit)
        self.assertIn("ProtectSystem=strict", unit)

    def test_vectors_ship_off_and_the_deadline_is_documented(self) -> None:
        content = _text("systemd/bocco-bridge.env.example")
        self.assertIn("BRIDGE_CONVERSATION_VECTORS=off", content)
        self.assertIn(
            "#BRIDGE_CONVERSATION_VECTOR_QUERY_DEADLINE_SECONDS=0.12", content
        )
        # The model name has to agree with the service's alias, so both files
        # must name it and the installer must place both.
        self.assertIn("multilingual-e5-small", content)
        self.assertIn(
            "multilingual-e5-small",
            _text("systemd/bocco-embeddings.env.example"),
        )

    def test_the_embedding_installer_pins_what_it_builds(self) -> None:
        installer = _text("scripts/install-embeddings.sh")
        # A tag, not a branch: the deadline was chosen against a measured
        # encode, and an unpinned build silently invalidates that measurement.
        self.assertIn("LLAMA_CPP_TAG=${LLAMA_CPP_TAG:-b", installer)
        self.assertNotIn("--branch master", installer)
        self.assertIn("multilingual-e5-small-q8_0.gguf", installer)
        self.assertIn("bocco-embeddings.service", installer)

    def test_the_embedding_installer_builds_something_that_survives_cleanup(
        self,
    ) -> None:
        installer = _text("scripts/install-embeddings.sh")
        # llama.cpp defaults to shared libraries. The installer copies one
        # binary and then deletes the build tree, so a dynamic build installs a
        # launcher whose libraries no longer exist and systemd restart-loops
        # with status=127. Observed on a clean Pi; this keeps the flag from
        # being tidied away.
        self.assertIn("-DBUILD_SHARED_LIBS=OFF", installer)
        self.assertIn('rm -rf "$BUILD_DIR"', installer)
        # And the binary is exercised before the script claims success.
        self.assertIn("/opt/bocco-embeddings/bin/llama-server --version", installer)

    def test_deployment_is_api_only(self) -> None:
        files = [
            REPOSITORY / "systemd/bocco-bridge.service",
            REPOSITORY / "systemd/bocco-bridge.env.example",
            REPOSITORY / "systemd/hermes.env.example",
            REPOSITORY / "hermes/config.example.yaml",
            REPOSITORY / "scripts/install-services.sh",
            REPOSITORY / "scripts/check-bridge.sh",
        ]
        combined = "\n".join(path.read_text(encoding="utf-8") for path in files)
        for obsolete in (
            "AGENT_WEBHOOK_TOKEN",
            "HERMES_WEBHOOK_SECRET",
            "DISCORD_BOT_TOKEN",
            "bocco-emo/SKILL.md",
            "127.0.0.1:8788",
            "127.0.0.1:8644",
        ):
            self.assertNotIn(obsolete, combined)
        checks = _text("scripts/check-bridge.sh")
        self.assertIn("127.0.0.1:8787/readyz", checks)
        self.assertIn("127.0.0.1:8642/health", checks)

    def test_examples_contain_no_credential_values(self) -> None:
        for name in ("bocco-bridge.env.example", "hermes.env.example"):
            content = (REPOSITORY / "systemd" / name).read_text(encoding="utf-8")
            self.assertNotIn("sk-", content)
            self.assertNotIn("Bot ", content)
            for line in content.splitlines():
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    if key.endswith(("TOKEN", "SECRET", "KEY", "UUID")):
                        self.assertEqual(value, "replace-at-install-time")

    def test_bridge_example_uses_durable_echo_correlation_by_default(self) -> None:
        content = _text("systemd/bocco-bridge.env.example")

        self.assertIn("BOCCO_BRIDGE_ECHO_WINDOW_SECONDS=600", content)
        self.assertIn("BOCCO_BRIDGE_RADAR_COOLDOWN_SECONDS=1800", content)
        self.assertNotIn("\nBOCCO_AGENT_USER_UUID=", content)

    def test_bridge_example_documents_optional_robot_persona(self) -> None:
        content = _text("systemd/bocco-bridge.env.example")

        self.assertIn("# BRIDGE_ROBOT_NICKNAME=コロン", content)
        self.assertIn("# BRIDGE_PERSONA=", content)
        self.assertIn("\\n", content)

    def test_bridge_example_enables_motion_only_acknowledgment(self) -> None:
        content = _text("systemd/bocco-bridge.env.example")

        self.assertIn("BRIDGE_ACK_MOTION=true", content)
        self.assertIn("BRIDGE_ACK_MOTION_NAME=ALRIGHT_N_0", content)

    def test_webhook_self_heal_ships_on_with_its_failure_explained(self) -> None:
        content = _text("systemd/bocco-bridge.env.example")

        # On by default, because the failure it covers is invisible and total.
        self.assertIn("BRIDGE_WEBHOOK_PROBE=on", content)
        # Every tuning knob commented out, so a replicator can find them
        # without any of them silently overriding a considered default.
        for name in (
            "BRIDGE_WEBHOOK_PROBE_SILENCE_SECONDS=21600",
            "BRIDGE_WEBHOOK_PROBE_TIMEOUT_SECONDS=20",
            "BRIDGE_WEBHOOK_PROBE_MAX_ATTEMPTS=3",
            "BRIDGE_WEBHOOK_PROBE_END_HOUR=22",
        ):
            self.assertIn(f"#{name}", content)
        # The example is where somebody meets this failure for the first time;
        # it has to say what it looks like, not just which variable to set.
        self.assertIn("delivers to it", content)
        # And it must say what it depends on, because BOCCO_ROOM_UUID ships
        # commented out and the probe is silently off without it.
        self.assertIn("Requires BOCCO_ROOM_UUID", content)

    def test_spoken_generation_and_compression_are_bounded(self) -> None:
        environment = _text("systemd/bocco-bridge.env.example")
        hermes_config = _text("hermes/config.example.yaml")

        self.assertIn("HERMES_MAX_OUTPUT_TOKENS=128", environment)
        self.assertIn("threshold_tokens: 8000", hermes_config)

    def test_fast_route_scripts_are_audited_and_installed_read_only(self) -> None:
        installer = _text("scripts/install-services.sh")
        for name in ("weather.py", "time_info.py", "news.py"):
            self.assertTrue((REPOSITORY / "hermes/fast-routes" / name).is_file())
            self.assertIn(name, installer)
        self.assertIn("/opt/hermes-agent/fast-routes", installer)
        self.assertIn(
            "/usr/local/share/bocco-bridge/fast-skills", installer
        )
        self.assertIn("-m 0555", installer)
        self.assertIn("-m 0644", installer)

        environment = _text("systemd/bocco-bridge.env.example")
        self.assertIn("BRIDGE_FAST_ROUTES=weather,time,news", environment)
        self.assertIn("DEFAULT_LOCATION=Tokyo", environment)


class HermesInstallerTests(unittest.TestCase):
    """The unit files above describe a Hermes that something has to install.

    These assert the installer that puts it there, because the failure mode
    when it is missing is a 203/EXEC restart loop rather than an error anyone
    can read.
    """

    def test_the_hermes_installer_pins_what_it_clones(self) -> None:
        installer = _text("scripts/install-hermes.sh")
        # A full commit sha, not a branch and not a tag. The provider-detection
        # workaround in config.example.yaml and the `gateway` entrypoint the
        # unit execs are both properties of one specific upstream revision.
        self.assertIn("HERMES_COMMIT=${HERMES_COMMIT:-", installer)
        self.assertIn("d1afa16053a3777849c2b5465d59a0147b2172f9", installer)
        self.assertIn("NousResearch/hermes-agent", installer)
        self.assertNotIn("--branch main", installer)
        self.assertNotIn("--branch master", installer)

    def test_the_hermes_installer_builds_what_the_unit_execs(self) -> None:
        installer = _text("scripts/install-hermes.sh")
        unit = _text("systemd/hermes-api.service")

        # Whatever path the unit execs is the path the installer must create.
        self.assertIn("/opt/hermes-agent/.venv/bin/python", unit)
        self.assertIn("hermes_cli.main", unit)
        self.assertIn('"$HERMES_PREFIX/.venv"', installer)
        self.assertIn("install --disable-pip-version-check -e", installer)
        # The identity the unit runs as, and the state directory it uses as HOME.
        self.assertIn("User=hermes-agent", unit)
        self.assertIn("useradd --system", installer)
        self.assertIn("Environment=HOME=/var/lib/hermes-agent", unit)
        self.assertIn("HERMES_STATE=${HERMES_STATE:-/var/lib/hermes-agent}", installer)

    def test_the_hermes_installer_verifies_before_declaring_success(self) -> None:
        installer = _text("scripts/install-hermes.sh")
        # install-embeddings.sh shipped without a post-install check and its
        # first failure surfaced as a systemd restart loop. Every one of these
        # is something hermes-api.service needs at exec time.
        self.assertIn("-m hermes_cli.main --help", installer)
        self.assertIn("weather/scripts/weather.py", installer)
        self.assertIn(".no-bundled-skills", installer)
        self.assertIn("provider: openai-api", installer)

    def test_service_installer_refuses_to_enable_a_hermes_that_is_absent(
        self,
    ) -> None:
        installer = _text("scripts/install-services.sh")
        # It enables hermes-api.service, so it must not merely warn.
        self.assertIn("if [ ! -x /opt/hermes-agent/.venv/bin/python ]", installer)
        self.assertIn("scripts/install-hermes.sh", installer)
        self.assertIn("exit 1", installer)

    def test_the_hermes_config_example_pins_the_model_provider(self) -> None:
        config = _text("hermes/config.example.yaml")
        # With no provider pinned, Hermes auto-detects OPENAI_API_KEY as an
        # OpenRouter key and every request 401s against api.openai.com. This
        # cost the project a debugging session; it must never regress to an
        # empty or provider-less config.
        self.assertIn("provider: openai-api", config)
        self.assertIn("default: gpt-5.6-luna", config)
        # Speech output: the bridge reads this text aloud, so no markdown.
        self.assertIn("api_server", config)
        self.assertIn("Plain text only", config)

    def test_custom_skills_are_versioned_rather_than_living_only_on_the_sd_card(
        self,
    ) -> None:
        # These five are written for this project. Nothing upstream provides
        # them: Japanese speech-shaped output, Japanese postal-code lookup,
        # NHK headlines, Reiwa era years, tsubo. Upstream skills (maps,
        # finance/stocks, health/fitness-nutrition) are deliberately NOT
        # vendored here — the installer copies those from the pinned checkout.
        expected = {
            "datetime": "datetime_tool.py",
            "email": "email_tool.py",
            "news": "news.py",
            "units": "units.py",
            "weather": "weather.py",
        }
        installer = _text("scripts/install-hermes.sh")
        for skill, script in expected.items():
            directory = REPOSITORY / "hermes/skills" / skill
            manifest = directory / "SKILL.md"
            self.assertTrue(manifest.is_file(), f"{skill}/SKILL.md missing")
            self.assertTrue(
                (directory / "scripts" / script).is_file(),
                f"{skill}/scripts/{script} missing",
            )
            # Provenance, so a later reader does not delete these believing
            # upstream would reinstall them.
            self.assertIn(
                "author: BOCCO Ambient Robot", manifest.read_text(encoding="utf-8")
            )
            self.assertIn(skill, installer)

    def test_installed_skills_match_what_the_deployment_calls(self) -> None:
        installer = _text("scripts/install-hermes.sh")
        # The three third-party skills come from the pinned checkout at the
        # paths upstream keeps them under; if upstream moves them, the
        # installer's own verification fails loudly rather than silently
        # installing eight skills and reporting success.
        self.assertIn("skills/productivity/maps", installer)
        self.assertIn("optional-skills/finance/stocks", installer)
        self.assertIn("optional-skills/health/fitness-nutrition", installer)

    def test_hermes_environment_example_documents_the_manual_key(self) -> None:
        content = _text("systemd/hermes.env.example")
        # The one step no script can do: the key costs money and belongs to a
        # human's account. The guide must not leave that as a silent gap.
        self.assertIn("platform.openai.com", content)
        self.assertIn("openssl rand -hex 32", content)
        # The weather skill's fallback location, which must agree with the
        # bridge's own DEFAULT_LOCATION for the fast route to match.
        self.assertIn("DEFAULT_LOCATION=Tokyo", content)


class _RecordingImapClient:
    """Stand-in for imaplib.IMAP4_SSL, recording the arguments of each SEARCH.

    The signature mirrors `imaplib.IMAP4.uid(self, command, *args)` exactly.
    That matters: `uid` has no charset parameter and forwards every argument to
    the command verbatim, so a fake that named one — `uid(self, command,
    charset, *criteria)` — would accept a call the real client turns into a
    malformed line, and the test would pass while the skill stayed broken.
    """

    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.searches: list[tuple[object, ...]] = []

    def uid(self, command: str, *args):
        assert command == "search"
        self.searches.append(tuple(args))
        outcome = self._responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _imap_command_line(*args: object) -> str:
    """Render the line imaplib would put on the wire for `IMAP4.uid(*args)`.

    Reproduces the argument handling in `imaplib.IMAP4._command`: `None` is
    dropped, `str` is encoded UTF-8, and the rest are joined with single
    spaces. Asserting on this is the difference between checking that an
    argument was passed and checking that the server receives a command it can
    parse.
    """

    rendered = []
    for arg in args:
        if arg is None:
            continue
        rendered.append(arg.decode("utf-8") if isinstance(arg, bytes) else str(arg))
    return " ".join(["UID", "SEARCH", *rendered[1:]])


class EmailSkillSearchTests(unittest.TestCase):
    """The rescued email skill searched IMAP for Japanese senders and failed.

    RFC 3501 requires a SEARCH to declare a charset once any key carries
    non-ASCII octets. The skill encoded `FROM "山田"` as UTF-8 and then told the
    server nothing about it, so a conforming server answered BAD.

    The declaration must be spelled out as the two tokens `CHARSET UTF-8`.
    `IMAP4.search` takes a charset argument and inserts the `CHARSET` keyword
    itself, but `IMAP4.uid` does not — it is a generic passthrough. Passing
    `"UTF-8"` alone in that position produces `UID SEARCH UTF-8 FROM "..."`,
    which is still malformed.
    """

    module = _load_skill_module(
        "hermes/skills/email/scripts/email_tool.py", "hermes_skill_email_tool"
    )

    def test_the_real_imaplib_signature_accepts_what_this_sends(self) -> None:
        # imaplib.IMAP4.uid takes (command, *args) with no charset parameter.
        # If that ever changes, the fake below stops representing reality.
        parameters = inspect.signature(imaplib.IMAP4.uid).parameters
        self.assertEqual(list(parameters), ["self", "command", "args"])
        self.assertEqual(parameters["args"].kind, inspect.Parameter.VAR_POSITIONAL)

    def test_ascii_search_declares_no_charset(self) -> None:
        client = _RecordingImapClient([("OK", [b"7 9"])])
        self.assertEqual(self.module._uid_search(client, "UNSEEN"), ["7", "9"])
        self.assertEqual(client.searches, [("UNSEEN",)])
        self.assertEqual(
            _imap_command_line("search", *client.searches[0]), "UID SEARCH UNSEEN"
        )

    def test_japanese_sender_search_declares_utf8_on_the_wire(self) -> None:
        criterion = self.module._sender_criterion("山田")
        client = _RecordingImapClient([("OK", [b"12"])])
        self.assertEqual(self.module._uid_search(client, criterion), ["12"])
        # The whole point: CHARSET is a keyword token of its own. A line reading
        # `UID SEARCH UTF-8 FROM "山田"` is what the first attempt at this fix
        # produced, and a conforming server rejects it exactly as it rejects the
        # undeclared form.
        self.assertEqual(
            _imap_command_line("search", *client.searches[0]),
            'UID SEARCH CHARSET UTF-8 FROM "山田"',
        )

    def test_a_server_that_rejects_the_charset_still_gets_an_answer(self) -> None:
        # Not every IMAP server implements SEARCH CHARSET UTF-8. A rejection
        # must fall back to the undeclared form — what this always did, and
        # what Gmail accepts — rather than turning a query into an error.
        criterion = self.module._sender_criterion("山田")
        for rejection in (imaplib.IMAP4.error("BAD"), ("NO", [b""])):
            with self.subTest(rejection=type(rejection).__name__):
                client = _RecordingImapClient([rejection, ("OK", [b"3"])])
                self.assertEqual(self.module._uid_search(client, criterion), ["3"])
                self.assertEqual(
                    [
                        _imap_command_line("search", *search)
                        for search in client.searches
                    ],
                    [
                        'UID SEARCH CHARSET UTF-8 FROM "山田"',
                        'UID SEARCH FROM "山田"',
                    ],
                )

    def test_a_search_that_fails_both_ways_is_reported_not_swallowed(self) -> None:
        criterion = self.module._sender_criterion("山田")
        client = _RecordingImapClient([("NO", [b""]), ("BAD", [b""])])
        with self.assertRaises(self.module.MailError):
            self.module._uid_search(client, criterion)
        self.assertEqual(len(client.searches), 2)

    def test_an_empty_result_is_not_retried_as_a_failure(self) -> None:
        # A sender with no mail is an OK response carrying an empty list, not
        # an error. Retrying it would double every fruitless search.
        criterion = self.module._sender_criterion("山田")
        client = _RecordingImapClient([("OK", [b""])])
        self.assertEqual(self.module._uid_search(client, criterion), [])
        self.assertEqual(len(client.searches), 1)


if __name__ == "__main__":
    unittest.main()
