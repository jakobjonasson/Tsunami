from __future__ import annotations

from pathlib import Path

import pytest

from tsunami.core.issues import TsunamiError
from tsunami.core.paths import AppPaths


def test_paths_come_from_environment():
    paths = AppPaths.from_env({"LOCALAPPDATA": r"C:\L", "APPDATA": r"C:\R"})
    assert paths.history_db == Path(r"C:\L\Tsunami\history.db")
    assert paths.known_hosts_file("acme") == Path(r"C:\L\Tsunami\known_hosts\acme")
    assert paths.policy_file == Path(r"C:\R\Tsunami\mcp-policy.yml")


def test_missing_environment_variable_is_an_error():
    with pytest.raises(TsunamiError, match="APPDATA"):
        AppPaths.from_env({"LOCALAPPDATA": r"C:\L"})


@pytest.mark.parametrize("customer", ["..", "../acme", "Acme", "acme\n", "", "-acme"])
def test_known_hosts_file_rejects_non_slugs(customer):
    paths = AppPaths.from_env({"LOCALAPPDATA": r"C:\L", "APPDATA": r"C:\R"})
    with pytest.raises(TsunamiError):
        paths.known_hosts_file(customer)
