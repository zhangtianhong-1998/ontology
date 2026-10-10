"""Literal unit identity preserves case-sensitive scientific symbols."""
import unicodedata


def unit_identity(value):
    """Normalize presentation only; MW and mW denote different scales."""
    return unicodedata.normalize("NFKC", str(value or "")).strip()
