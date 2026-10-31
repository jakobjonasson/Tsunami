"""Which saved logins a customer uses, and which its plays may push with login().

Login names are global, but each customer may use only some of them. A play may push a
login (login() in a template, or a `login` prompt) only if its customer allows it:

- the logins the customer logs in with: its own, its sites' defaults and its devices'.
  Not its jump host's: that is a login for another machine, and pushing it would copy it
  onto every device;
- its `push_logins`: accounts its plays may push that Tsunami never logs in with.

Every refusal reads the same, whether the login does not exist, belongs to another
customer, or to a blocked one, so trying names never tells them apart.

See design.md, Snippets and rendering: login().
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from tsunami.core.issues import TsunamiError


class LoginRefError(TsunamiError):
    """A login() call or a login prompt names a login the customer may not push."""


def customer_logins(customer: Any) -> set[str]:
    """The logins a customer (core.workspace.CustomerInventory) logs in to devices with."""
    cfg = customer.config
    names = {cfg.credential, *cfg.site_credentials.values()}
    names.update(d.credential for d in customer.devices if d.credential)
    return names


def allowed_logins(customer: Any) -> frozenset[str]:
    """The logins a customer's plays may push: the ones it logs in with, plus push_logins."""
    return frozenset(customer_logins(customer) | set(customer.config.push_logins))


def referenced_logins(customers: Iterable[Any]) -> set[str]:
    """Every login any of these customers refers to: logins, jump hosts and push_logins."""
    names: set[str] = set()
    for customer in customers:
        names |= allowed_logins(customer)
        if customer.config.jump_host is not None:
            names.add(customer.config.jump_host.credential)
    return names


def refusal(name: object, customer: str) -> str:
    """The one message for a login a customer may not push."""
    return (
        f"login {name!r} is not available to customer {customer!r}: use one of its logins, "
        "or add it to push_logins in its customer settings"
    )


def check_allowed(name: object, customer: Any) -> str:
    """name, if the customer may push it; LoginRefError otherwise."""
    if not isinstance(name, str) or name not in allowed_logins(customer):
        raise LoginRefError(refusal(name, customer.slug))
    return name


@dataclass(frozen=True)
class LoginView:
    """What a render may know about a saved login: never the password."""

    username: str
    has_enable: bool


def read_login(secrets: Any, name: str) -> LoginView:
    """The login's username and whether it has an enable value. The password is dropped
    here: it is read again only in apply mode, as secret() values are."""
    credential = secrets.get_login(name)  # TsunamiError if locked or missing
    return LoginView(credential.username, bool(credential.enable))
