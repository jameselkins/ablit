"""Read account credentials from the environment, process memory or OS keyring."""

from __future__ import annotations

import getpass
from collections.abc import Mapping

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

from ablit.backends.base import Creds, creds_from_env
from ablit.backends.errors import CredentialsRejected


class CredentialError(Exception):
    """A credential is missing or could not be saved securely."""


SESSION: dict[str, str] = {}


def secret_get(name: str) -> str | None:
    try:
        return keyring.get_password("ablit", name)
    except KeyringError as error:
        raise CredentialError("The OS keyring is unavailable.") from error


def secret_set(name: str, value: str) -> None:
    try:
        keyring.set_password("ablit", name, value)
    except KeyringError as error:
        raise CredentialError("The OS keyring could not save the secret.") from error


def secret_delete(name: str) -> None:
    try:
        keyring.delete_password("ablit", name)
    except PasswordDeleteError:
        pass
    except KeyringError as error:
        raise CredentialError("The OS keyring could not delete the secret.") from error


def credentials(backend: str, environ: Mapping[str, str] | None = None) -> Creds:
    env = creds_from_env(backend, environ)
    if env:
        return env
    value = SESSION.get(backend) or secret_get(f"accounts/{backend}")
    if not value:
        raise CredentialError(f"No {backend} key. Run `ablit setup {backend}` or set VAST_API_KEY.")
    return Creds(value)


def setup_vast(driver, store, no_referral: bool, environ=None) -> int:
    from ablit.referral import load_ids, referral_enabled, setup_choices
    from ablit.runs.store import atomic_json

    config_path = store.root / "config.json"
    import json
    config = json.loads(config_path.read_text()) if config_path.exists() else {}
    if no_referral or not referral_enabled(environ):
        config["referral"] = False
        atomic_json(config_path, config)
    if input("Do you already have an account at Vast.ai? [y/N] ").strip().lower() not in ("y", "yes"):
        disclosure, choices = setup_choices("vast", load_ids(), referral_enabled(environ, config.get("referral")))
        if disclosure:
            print(disclosure)
        for choice in choices:
            print(f"[{choice.key}] {choice.label}")
        answer = input("Choose: ").strip()
        if answer == "3":
            return 0
        if answer == "2":
            print("https://cloud.vast.ai/")
            return 0
        if answer == "1" and disclosure:
            return 0
        raise CredentialError("No signup choice was selected.")
    key = getpass.getpass("Vast API key (hidden): ").strip()
    if not key:
        raise CredentialError("No key was entered.")
    try:
        driver.test_credentials(Creds(key))
    except CredentialsRejected:
        raise
    except Exception as error:
        raise CredentialError(f"The key test failed before Vast accepted or refused the key: {error}") from error
    if input("Save in the OS keyring? [y/N, N keeps it only in this process] ").strip().lower() in ("y", "yes"):
        secret_set("accounts/vast", key)
        print("Saved the Vast key in the OS keyring.")
    else:
        SESSION["vast"] = key
        print("The key is kept only in this process and expires when setup exits. "
              "For a shell session, set VAST_API_KEY yourself with hidden input.")
    return 0
