"""Static regression checks for ``infra/flex.bicep`` (#768).

The Flex Consumption template backs the optional ``certify_2x`` lane of ``e2e-azure.yml``. The first
real dispatch of that lane failed in the Bicep step, because the template used a deployment storage
authentication type that Azure does not accept. These checks need no Azure access; they pin the
documented contract so a template edit cannot reintroduce that class of error. They do not replace
the live lane: only a green dispatch proves the template deploys.

Contract source: Microsoft Learn, "Automate function app resource deployment to Azure" (Flex
Consumption) and the "Azure Functions app settings reference" (settings the platform manages).
"""

from __future__ import annotations

from pathlib import Path
import re

BICEP = Path(__file__).resolve().parents[1] / "infra" / "flex.bicep"
TEXT = BICEP.read_text()

# Documented ``functionAppConfig.deployment.storage.authentication.type`` values.
VALID_AUTH_TYPES = {
    "SystemAssignedIdentity",
    "UserAssignedIdentity",
    "StorageAccountConnectionString",
}
# App settings the Flex Consumption platform owns; they must not be set by the template.
PLATFORM_MANAGED_SETTINGS = {"FUNCTIONS_EXTENSION_VERSION", "FUNCTIONS_WORKER_RUNTIME"}


def _code() -> str:
    """The template without ``//`` comments, so prose cannot satisfy or break a check."""
    return "\n".join(re.sub(r"//.*$", "", line) for line in TEXT.splitlines())


def _app_setting_names() -> set[str]:
    return set(re.findall(r"\{\s*name:\s*'([A-Za-z0-9_]+)'\s*,\s*value:", _code()))


def test_deployment_storage_authentication_type_is_a_documented_value() -> None:
    # Given: the authentication block of the deployment storage configuration
    match = re.search(r"authentication:\s*\{[^}]*?type:\s*'([^']+)'", _code(), re.S)

    # Then: its type is one of the values Azure accepts
    assert match is not None
    assert match.group(1) in VALID_AUTH_TYPES


def test_connection_string_authentication_names_an_existing_app_setting() -> None:
    # Given: the setting name the deployment storage authentication refers to
    match = re.search(r"storageAccountConnectionStringName:\s*'([^']+)'", _code())

    # Then: the template defines that app setting (it "must already exist" per Microsoft Learn)
    assert match is not None
    assert match.group(1) in _app_setting_names()


def test_platform_managed_settings_are_not_set_as_app_settings() -> None:
    assert _app_setting_names() & PLATFORM_MANAGED_SETTINGS == set()


def test_deployment_container_is_created_before_the_site() -> None:
    # Given: the site's deployment package location
    value = re.search(r"value:\s*'(\$\{storageAccount[^']*)'", _code())

    # Then: it references the container resource, so ARM orders its creation first
    assert value is not None
    assert "deploymentContainer.name" in value.group(1)
    assert "resource deploymentContainer " in _code()
