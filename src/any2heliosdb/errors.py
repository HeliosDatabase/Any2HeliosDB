"""Exception hierarchy for Any2HeliosDB.

Mirrors the shape of the reference Rust toolkit's ``MigrationError`` while
staying Pythonic. All tool errors derive from :class:`Any2HeliosError` so the
CLI can present a single clean failure surface.
"""
from __future__ import annotations


class Any2HeliosError(Exception):
    """Base class for every error raised by the tool."""


class ConfigError(Any2HeliosError):
    """Invalid/missing configuration or wizard input."""


class SourceConnectionError(Any2HeliosError):
    """Could not connect to or authenticate against the source database."""


class TargetConnectionError(Any2HeliosError):
    """Could not connect to or authenticate against the HeliosDB target."""


class IntrospectionError(Any2HeliosError):
    """Failed while reading the source catalog (often a privilege problem)."""


class TypeMappingError(Any2HeliosError):
    """A source data type could not be mapped and had no override."""


class TranslationError(Any2HeliosError):
    """PL/SQL or SQL translation failed in a way the tool cannot work around."""


class LoadError(Any2HeliosError):
    """Data load (COPY/INSERT) into the target failed."""


class ValidationError(Any2HeliosError):
    """A TEST / TEST_COUNT / TEST_DATA check failed."""


class ResumeError(Any2HeliosError):
    """The run manifest is inconsistent or cannot be resumed."""


class CapabilityError(Any2HeliosError):
    """The target lacks a capability required for the requested operation."""
