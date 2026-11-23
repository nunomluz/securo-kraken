import json
import re
from pathlib import Path

from securo_kraken import assets, client, payload

ROOT = Path(__file__).resolve().parent.parent


def _manifest():
    return json.loads((ROOT / "securo-plugin.json").read_text("utf-8"))


def test_manifest_matches_code():
    m = _manifest()
    assert {n["host"] for n in m["network"]} == {client.HOST} == {"api.kraken.com"}
    assert m["runtime"]["entrypoint"] == "securo_kraken.plugin:plugin"
    assert re.fullmatch(r"nunomluz/securo-kraken", m["id"])
    # Releases bump both files together (AGENTS.md, "Releasing").
    pyproject = (ROOT / "pyproject.toml").read_text("utf-8")
    assert re.search(rf'^version = "{re.escape(m["version"])}"$', pyproject, re.M)
    assert re.fullmatch(r"\d+\.\d+\.\d+", m["version"]) and m["min_securo_version"] == "0.20.0"
    assert "api_scopes" not in m
    assert payload.LOGO in m["logos"] and (ROOT / m["logos"][payload.LOGO]).is_file()


def test_integration_fields():
    integ = _manifest()["contributes"]["integrations"][0]
    assert integ["key"] == "api" and integ["auth"] == "credentials"
    fields = {f["key"]: f for f in integ["fields"]}
    assert set(fields) == {"api_key", "api_secret", "currency"}
    assert fields["api_key"]["secret"] and fields["api_secret"]["secret"]
    assert fields["currency"]["required"] is False and not fields["currency"].get("secret")
    for code in assets.VALUATIONS:
        assert code in fields["currency"]["label"]["en"]
    assert integ["provides"] == ["accounts", "transactions", "holdings", "trades"]
    assert integ["sync_minutes"] == 60


def test_description_names_both_permissions():
    integ = _manifest()["contributes"]["integrations"][0]
    for lang in ("en", "pt"):
        text = integ["description"][lang]
        assert "Query Funds" in text and "Query Ledger Entries" in text


def test_icons_are_small_pngs():
    for p in ("icon.png", "logos/kraken.png"):
        data = (ROOT / p).read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) <= 64 * 1024
