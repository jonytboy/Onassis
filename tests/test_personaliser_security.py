"""The Personaliser must be reachable by buyers with no API key."""

from onassis.security import _PUBLIC_PREFIXES


def test_make_routes_are_public():
    from onassis import security as S
    is_public = S.SecurityMiddleware._is_public if hasattr(S, "SecurityMiddleware") else None
    if is_public is None:                       # fall back to the prefix table itself
        assert any("/make/pet-portrait".startswith(p) for p in _PUBLIC_PREFIXES)
        return
    assert is_public("/make")
    assert is_public("/make/pet-portrait")
    assert is_public("/make/download/abc123")
    assert not is_public("/operations/check")   # protected routes stay protected
