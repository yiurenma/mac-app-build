"""Offline enterprise pip configuration validation tests (no Nexus requests)."""
import os
import subprocess
import sys
import unittest
from pathlib import Path

from appbuilder.pip_sources import PipSourceError, configured_value, validate_sources


class PipSourceTests(unittest.TestCase):
    def setUp(self):
        self.conf = {
            "global.index-url": "https://nexus02.systems.uk.hsbc:8081/nexus/repository/pypi-proxy_n3p/simple",
            "global.extra-index-url": "https://nexus02.systems.uk.hsbc:8081/nexus/repository/pypi-hosted_n3-prd/simple",
            "global.cert": "/some/corporate/CA.pem",
        }
        self.get = lambda key: self.conf.get(key, "")

    def test_two_corporate_nexus_indexes_allowed(self):
        self.assertEqual(validate_sources({}, self.get), (1, 1))

    def test_no_extra_index_allowed(self):
        del self.conf["global.extra-index-url"]
        self.assertEqual(validate_sources({}, self.get), (1, 0))

    def test_extra_public_index_blocked(self):
        self.conf["global.extra-index-url"] = "https://pypi.org/simple"
        with self.assertRaises(PipSourceError):
            validate_sources({}, self.get)

    def test_env_extra_public_overrides_corporate_config_and_blocked(self):
        with self.assertRaises(PipSourceError):
            validate_sources({"PIP_EXTRA_INDEX_URL": "https://pypi.org/simple"}, self.get)

    def test_non_https_index_blocked(self):
        self.conf["global.index-url"] = "http://nexus02.systems.uk.hsbc/simple"
        with self.assertRaises(PipSourceError):
            validate_sources({}, self.get)

    def test_lookalike_host_not_allowed(self):
        self.conf["global.index-url"] = "https://nexus02.systems.uk.hsbc.attacker.org/simple"
        with self.assertRaises(PipSourceError):
            validate_sources({}, self.get)

    def test_missing_index_is_not_public_fallback(self):
        del self.conf["global.index-url"]
        with self.assertRaises(PipSourceError):
            validate_sources({}, self.get)

    def test_install_section_override(self):
        self.conf["install.index-url"] = "https://another.systems.uk.hsbc/simple"
        self.assertEqual(configured_value("index-url", {}, self.get), self.conf["install.index-url"])

    def test_env_override_comes_first(self):
        val = "https://override.systems.uk.hsbc/simple"
        self.assertEqual(configured_value("index-url", {"PIP_INDEX_URL": val}, self.get), val)

    def test_explicit_allowed_corporate_alternate_host(self):
        self.conf["global.index-url"] = "https://nexus.corp.company.internal/simple"
        with self.assertRaises(PipSourceError):
            validate_sources({}, self.get)
        self.assertEqual(validate_sources({"APP_BUILDER_ALLOWED_PIP_HOSTS": "nexus.corp.company.internal"}, self.get), (1, 1))

    def test_multiple_extra_indexes_validated_individually(self):
        self.conf["global.extra-index-url"] = "https://mirror1.systems.uk.hsbc/simple https://pypi.org/simple"
        with self.assertRaises(PipSourceError):
            validate_sources({}, self.get)

    def test_no_index_option_rejected(self):
        with self.assertRaises(PipSourceError):
            validate_sources({"PIP_NO_INDEX": "true"}, self.get)

    def test_credentials_not_in_error(self):
        self.conf["global.extra-index-url"] = "https://user:ultrasecret@public-repo.example/simple"
        with self.assertRaises(PipSourceError) as ctx:
            validate_sources({}, self.get)
        self.assertNotIn("ultrasecret", str(ctx.exception))

    def test_installer_does_not_upgrade_or_modify_pip_config(self):
        installer = (Path(__file__).resolve().parents[1] / "install.sh").read_text()
        self.assertIn("python3 -m appbuilder.pip_sources", installer)
        self.assertNotIn("pip install --upgrade", installer)
        self.assertNotIn("pip config unset", installer)
        self.assertNotIn("pip config set", installer)
        self.assertIn(".venv/bin/python -m pip --disable-pip-version-check install -r requirements.txt", installer)


if __name__ == "__main__":
    unittest.main()
