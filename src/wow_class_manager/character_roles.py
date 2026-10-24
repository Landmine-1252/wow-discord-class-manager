"""Gameplay role policy, independent of Discord's cosmetic class roles."""

from .classes import GAMEPLAY_ROLES, REGISTRY, supports_gameplay_role
from .roles import UserError


def role_name(gameplay_role: str | None) -> str:
    return GAMEPLAY_ROLES.get(gameplay_role, "Role not set")


def validate_gameplay_role(class_id: str, gameplay_role: str | None) -> None:
    definition = REGISTRY.get(class_id)
    if definition is None:
        raise UserError("That character class is unknown. Choose an enabled class.")
    if gameplay_role not in GAMEPLAY_ROLES:
        raise UserError("Choose a gameplay role: DPS, Healing or Tank.")
    if not supports_gameplay_role(class_id, gameplay_role):
        allowed = ", ".join(GAMEPLAY_ROLES[role] for role in definition.gameplay_roles)
        raise UserError(
            f"{definition.name} cannot select {role_name(gameplay_role)}. Choose {allowed}."
        )
