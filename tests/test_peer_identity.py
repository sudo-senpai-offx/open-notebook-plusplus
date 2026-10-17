"""Per-device peer credentials.

The load-bearing test in this file is
`test_revoking_one_peer_leaves_the_others_working`. It is the only reason
per-device tokens exist: the shared cluster secret this replaced could not
evict one device without evicting all of them, so a lost phone took the PC's
access down with it.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from open_notebook.peers.identity import (
    ensure_identity,
    generate_device_id,
    generate_token,
    load_identity,
    set_identity_name,
)
from open_notebook.peers.registry import (
    add_peer,
    load_registry,
    pairing_payload,
    remove_peer,
    save_registry,
)
from open_notebook.peers.urls import (
    InvalidPeerURL,
    normalize_base_url,
    validate_peer_url,
)


@pytest.fixture
def identity_path(tmp_path: Path) -> Path:
    return tmp_path / "identity.json"


@pytest.fixture
def registry_path(tmp_path: Path) -> Path:
    return tmp_path / "registry.json"


# ---------------------------------------------------------------- identity


class TestDeviceIdentity:
    def test_generated_once_and_stable(self, identity_path: Path):
        first = ensure_identity(identity_path, name="pc")
        second = ensure_identity(identity_path, name="pc")

        assert first.device_id == second.device_id
        assert first.token == second.token, "a reboot must not invalidate the peer"

    def test_id_is_guessable_by_nobody_else(self, identity_path: Path):
        one = ensure_identity(identity_path)
        two = ensure_identity(identity_path.with_name("other.json"))

        assert one.device_id != two.device_id
        assert one.token != two.token

    def test_token_has_real_entropy(self):
        assert len(generate_token()) >= 40
        assert len({generate_token() for _ in range(50)}) == 50

    def test_file_is_owner_only(self, identity_path: Path):
        """The file holds a bearer credential for this device's whole identity."""
        ensure_identity(identity_path, name="pc")

        mode = stat.S_IMODE(identity_path.stat().st_mode)
        assert mode == 0o600, f"identity file is {mode:o}, expected 600"

    def test_rewrite_narrows_permissions_on_an_already_readable_file(
        self, identity_path: Path
    ):
        """A file left readable by an earlier version must not stay readable.

        `Path.write_text` keeps the existing mode, so the narrowing is
        explicit. Exercised through the rename path, which is a real rewrite
        of an existing identity; `ensure_identity` would refuse to overwrite.
        """
        ensure_identity(identity_path, name="pc")
        identity_path.chmod(0o644)
        assert stat.S_IMODE(identity_path.stat().st_mode) == 0o644

        set_identity_name("phone", identity_path)

        assert stat.S_IMODE(identity_path.stat().st_mode) == 0o600

    def test_naming_keeps_the_credential(self, identity_path: Path):
        before = ensure_identity(identity_path)
        renamed = set_identity_name("phone", identity_path)

        assert renamed.token == before.token
        assert renamed.device_id == before.device_id
        assert renamed.name == "phone"

    def test_corrupt_file_is_not_silently_regenerated(self, identity_path: Path):
        """Regenerating would hand this device a new id while its peer still
        held the old token: every call would fail as 'unknown device', with
        nothing logged that explains why."""
        identity_path.parent.mkdir(parents=True, exist_ok=True)
        identity_path.write_text("{not json")

        with pytest.raises(RuntimeError, match="not valid JSON"):
            load_identity(identity_path)

    def test_missing_fields_are_refused_not_guessed(self, identity_path: Path):
        identity_path.parent.mkdir(parents=True, exist_ok=True)
        identity_path.write_text(json.dumps({"device_id": "dev-1"}))

        with pytest.raises(RuntimeError, match="missing device_id or token"):
            load_identity(identity_path)

    def test_public_view_omits_the_token(self, identity_path: Path):
        """Any endpoint reporting who is registered must not echo credentials."""
        identity = ensure_identity(identity_path, name="pc")

        assert "token" not in identity.public()
        assert identity.public()["device_id"] == identity.device_id

    def test_pairing_payload_carries_the_token_once(self, identity_path: Path):
        """The deliberate one-time handover, and the only place it leaves."""
        payload = pairing_payload(identity_path=identity_path)

        assert payload["token"]
        assert payload["device_id"].startswith("dev-")

    def test_device_id_is_prefixed_for_logs(self):
        assert generate_device_id().startswith("dev-")


# ---------------------------------------------------------------- registry


class TestPeerRegistry:
    def test_a_paired_device_verifies(self, registry_path: Path):
        add_peer(
            "dev-phone", "tok-phone", ["http://127.0.0.1:15055"], path=registry_path
        )

        assert load_registry(registry_path).verify_peer("dev-phone", "tok-phone")

    def test_wrong_token_is_refused(self, registry_path: Path):
        add_peer(
            "dev-phone", "tok-phone", ["http://127.0.0.1:15055"], path=registry_path
        )

        assert not load_registry(registry_path).verify_peer("dev-phone", "tok-wrong")

    def test_unknown_device_is_refused(self, registry_path: Path):
        add_peer(
            "dev-phone", "tok-phone", ["http://127.0.0.1:15055"], path=registry_path
        )

        assert not load_registry(registry_path).verify_peer("dev-nobody", "tok-phone")

    def test_empty_credentials_are_refused(self, registry_path: Path):
        add_peer(
            "dev-phone", "tok-phone", ["http://127.0.0.1:15055"], path=registry_path
        )
        registry = load_registry(registry_path)

        assert not registry.verify_peer("dev-phone", "")
        assert not registry.verify_peer("", "tok-phone")
        assert not registry.verify_peer(None, None)

    def test_token_is_generated_when_not_supplied(self, registry_path: Path):
        """The usual case: this device issues the credential, then hands it over."""
        entry = add_peer(
            "dev-phone", base_urls=["http://127.0.0.1:15055"], path=registry_path
        )

        assert entry.token
        assert load_registry(registry_path).verify_peer("dev-phone", entry.token)

    def test_empty_token_is_refused(self, registry_path: Path):
        """An empty token would pair a device whose credential is `""` - which
        anything that omits the header could present."""
        with pytest.raises(ValueError, match="empty"):
            add_peer("dev-phone", token="   ", path=registry_path)

    def test_blank_device_id_is_refused(self, registry_path: Path):
        with pytest.raises(ValueError, match="device_id is required"):
            add_peer("  ", token="t", path=registry_path)

    # -- the property this design exists for ----------------------------

    def test_revoking_one_peer_leaves_the_others_working(self, registry_path: Path):
        """The reason per-device tokens replaced a cluster secret.

        With one shared key, evicting the lost phone meant rotating the key
        and taking the PC's access down with it.
        """
        add_peer(
            "dev-phone", "tok-phone", ["http://127.0.0.1:15055"], path=registry_path
        )
        add_peer("dev-pc", "tok-pc", ["http://127.0.0.1:15056"], path=registry_path)

        assert remove_peer("dev-phone", path=registry_path)

        registry = load_registry(registry_path)
        assert not registry.verify_peer("dev-phone", "tok-phone"), "phone still trusted"
        assert registry.verify_peer("dev-pc", "tok-pc"), "PC collateral damage"

    def test_revoking_twice_reports_that_there_was_nothing_to_remove(
        self, registry_path: Path
    ):
        """So an idempotent script can tell a second pass from a typo in the id."""
        add_peer("dev-phone", "tok-phone", path=registry_path)

        assert remove_peer("dev-phone", path=registry_path)
        assert not remove_peer("dev-phone", path=registry_path)

    def test_revocation_is_remembered_as_deliberate(self, registry_path: Path):
        """'Never heard of it' and 'we removed it on purpose' are different
        events, and the second is worth noticing."""
        add_peer("dev-phone", "tok-phone", path=registry_path)
        remove_peer("dev-phone", path=registry_path)

        registry = load_registry(registry_path)
        assert registry.is_revoked("dev-phone")
        assert not registry.is_revoked("dev-pc")

    def test_re_pairing_clears_the_revocation(self, registry_path: Path):
        add_peer("dev-phone", "tok-phone", path=registry_path)
        remove_peer("dev-phone", path=registry_path)
        add_peer("dev-phone", "tok-new", path=registry_path)

        registry = load_registry(registry_path)
        assert not registry.is_revoked("dev-phone")
        assert registry.verify_peer("dev-phone", "tok-new")
        assert not registry.verify_peer("dev-phone", "tok-phone")

    def test_re_pairing_with_a_new_token_revokes_the_old_one(self, registry_path: Path):
        """The recovery path for a device whose file was lost."""
        add_peer("dev-phone", "tok-old", path=registry_path)
        add_peer("dev-phone", "tok-new", path=registry_path)

        registry = load_registry(registry_path)
        assert not registry.verify_peer("dev-phone", "tok-old")
        assert registry.verify_peer("dev-phone", "tok-new")

    def test_addresses_are_replaced_not_merged(self, registry_path: Path):
        """A moved device keeps a stale address otherwise, and we would keep
        accepting traffic for wherever it used to be."""
        add_peer("dev-phone", "tok", ["http://10.7.109.219:5055"], path=registry_path)
        add_peer("dev-phone", "tok", ["http://10.158.2.78:5055"], path=registry_path)

        registry = load_registry(registry_path)
        entry = registry.get("dev-phone")
        assert entry is not None
        assert entry.base_urls == ["http://10.158.2.78:5055"]

    def test_listing_never_exposes_tokens(self, registry_path: Path):
        add_peer(
            "dev-phone", "tok-phone", ["http://127.0.0.1:15055"], path=registry_path
        )

        blob = json.dumps(load_registry(registry_path).listing())

        assert "tok-phone" not in blob
        assert "dev-phone" in blob

    def test_registry_file_is_owner_only(self, registry_path: Path):
        add_peer("dev-phone", "tok-phone", path=registry_path)

        mode = stat.S_IMODE(registry_path.stat().st_mode)
        assert mode == 0o600, f"registry holds bearer tokens but is {mode:o}"

    def test_credential_files_are_never_synced(self):
        """They live under DATA_FOLDER, which the sync repo never carries."""
        from open_notebook.peers.identity import IDENTITY_FILE
        from open_notebook.peers.registry import REGISTRY_FILE

        for path in (IDENTITY_FILE, REGISTRY_FILE):
            assert "sync" not in path.parts, f"{path} sits inside the sync tree"


# ------------------------------------------------------- address matching


class TestPeerForUrl:
    def test_resolves_a_registered_address(self, registry_path: Path):
        add_peer("dev-phone", "tok", ["http://127.0.0.1:15055"], path=registry_path)

        entry = load_registry(registry_path).peer_for_url("http://127.0.0.1:15055")

        assert entry is not None and entry.device_id == "dev-phone"

    def test_trailing_slash_and_case_do_not_defeat_matching(self, registry_path: Path):
        """A missed match here refuses a legitimate device, which looks
        exactly like the device being down."""
        add_peer("dev-phone", "tok", ["http://127.0.0.1:15055"], path=registry_path)
        registry = load_registry(registry_path)

        assert registry.peer_for_url("http://127.0.0.1:15055/") is not None
        assert registry.peer_for_url("HTTP://127.0.0.1:15055") is not None

    def test_unregistered_address_resolves_to_nothing(self, registry_path: Path):
        """This is what makes refusing to fetch from it a default."""
        add_peer("dev-phone", "tok", ["http://127.0.0.1:15055"], path=registry_path)

        assert (
            load_registry(registry_path).peer_for_url("http://169.254.169.254") is None
        )
        assert load_registry(registry_path).peer_for_url("http://10.0.0.5:9999") is None

    def test_a_revoked_peer_no_longer_serves_its_address(self, registry_path: Path):
        add_peer("dev-phone", "tok", ["http://127.0.0.1:15055"], path=registry_path)
        remove_peer("dev-phone", path=registry_path)

        assert (
            load_registry(registry_path).peer_for_url("http://127.0.0.1:15055") is None
        )


# ------------------------------------------------------------ url policy


class TestPeerURLPolicy:
    @pytest.mark.parametrize(
        "url",
        [
            "http://169.254.169.254/latest/meta-data/",
            "http://169.254.169.254:80/",
            "http://[fe80::1]/",
        ],
    )
    def test_metadata_and_link_local_are_refused(self, url: str):
        """169.254.169.254 is the highest-value address in the list: on a
        hosted VM one request can return credentials for the whole account,
        and it is reachable from every workload by default."""
        with pytest.raises(InvalidPeerURL):
            validate_peer_url(url)

    def test_link_local_refusal_says_why(self):
        with pytest.raises(InvalidPeerURL, match="metadata"):
            validate_peer_url("http://169.254.169.254/")

    @pytest.mark.parametrize(
        "url, reason",
        [
            ("file:///etc/passwd", "scheme"),
            ("gopher://127.0.0.1:70/", "scheme"),
            ("ftp://127.0.0.1/", "scheme"),
            ("http://user:pass@10.7.109.219:5055", "credentials"),
            ("http://:5055/", "host"),
            ("127.0.0.1:15055", "scheme"),
            ("", "empty"),
        ],
    )
    def test_malformed_or_dangerous_shapes_are_refused(self, url: str, reason: str):
        with pytest.raises(InvalidPeerURL, match=reason):
            validate_peer_url(url)

    def test_multicast_and_unspecified_are_refused(self):
        with pytest.raises(InvalidPeerURL, match="multicast"):
            validate_peer_url("http://224.0.0.1:5055/")
        with pytest.raises(InvalidPeerURL, match="unspecified"):
            validate_peer_url("http://0.0.0.0:5055/")

    @pytest.mark.parametrize(
        "url, expected",
        [
            # Loopback is allowed on purpose: the SSH tunnel between the two
            # devices ends on it, so forbidding it would forbid the only
            # topology in use. See the module docstring.
            ("http://127.0.0.1:15055", "http://127.0.0.1:15055"),
            ("http://127.0.0.1:15055/", "http://127.0.0.1:15055"),
            ("http://localhost:5055", "http://localhost:5055"),
            ("http://10.7.109.219:5055/", "http://10.7.109.219:5055"),
            ("https://phone.local", "https://phone.local"),
        ],
    )
    def test_the_topology_actually_in_use_is_allowed(self, url: str, expected: str):
        assert validate_peer_url(url) == expected

    def test_default_ports_are_canonicalised_away(self):
        """Otherwise `http://pc:80` and `http://pc` are different peers."""
        assert normalize_base_url("http://10.0.0.5:80/") == "http://10.0.0.5"
        assert normalize_base_url("https://10.0.0.5:443") == "https://10.0.0.5"

    def test_a_non_default_port_is_kept(self):
        assert normalize_base_url("http://10.0.0.5:5055") == "http://10.0.0.5:5055"

    def test_ipv6_literal_stays_bracketed(self):
        assert normalize_base_url("http://[::1]:5055/") == "http://[::1]:5055"

    def test_hostnames_are_not_resolved_here(self):
        """A name that resolves to a blocked address passes this layer.

        Deliberate: resolving here would mean resolving twice, and the second
        resolution - the one that connects - is the attacker's to control.
        The registry allowlist is what actually pins the destination.
        """
        assert (
            normalize_base_url("http://metadata.internal/")
            == "http://metadata.internal"
        )

    def test_registering_a_dangerous_address_is_refused(self, registry_path: Path):
        """The policy applies when pairing too, not only to request bodies."""
        with pytest.raises(ValueError, match="cannot register"):
            add_peer("dev-evil", "tok", ["http://169.254.169.254/"], path=registry_path)


# ------------------------------------------------------------ persistence


class TestPersistence:
    def test_registry_survives_a_round_trip(self, registry_path: Path):
        add_peer(
            "dev-phone", "tok-phone", ["http://127.0.0.1:15055"], path=registry_path
        )

        reloaded = load_registry(registry_path)

        assert reloaded.verify_peer("dev-phone", "tok-phone")
        assert reloaded.peer_for_url("http://127.0.0.1:15055") is not None

    def test_empty_registry_when_never_paired(self, registry_path: Path):
        registry = load_registry(registry_path)

        assert registry.peers == {}
        assert registry.verify_peer("dev-phone", "tok-phone") is False

    def test_save_creates_owner_only_permissions(self, tmp_path: Path):
        path = tmp_path / "nested" / "registry.json"
        save_registry(load_registry(), path)

        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
