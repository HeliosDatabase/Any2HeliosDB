"""Any2HeliosDB — migrate Oracle / MySQL / PostgreSQL / SQL Server into HeliosDB or stock PostgreSQL.

A modern, Python successor to Ora2Pg, retargeted at HeliosDB (Lite, Full, and
— via the portable psycopg/PG-wire path — Nano). The guiding principle is to
prefer fixing/extending the target database over carrying translation logic in
the tool, so the fork stays thin. Every incompatibility the tool works around is
also emitted as a structured target-gap report.

Importing this package is side-effect free and does not pull in any database
driver; heavy imports (psycopg, oracledb, …) are deferred to the modules that
actually open connections, so the pure-logic layers stay unit-testable without
the drivers installed.
"""

__version__ = "0.9.2"
__all__ = ["__version__"]
