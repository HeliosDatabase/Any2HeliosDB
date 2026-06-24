"""Create a representative SQL Server sample schema for integration testing.

Mirrors tests/fixtures/mysql_sample.py / oracle_sample.py (same shape/data) so
MSSQL->HeliosDB can be validated to the same parity. Idempotent. Targets a local
SQL Server reached over ODBC:
    host=127.0.0.1 port=14433 user=sa password=Strong!Passw0rd db=hr schema=dbo

Exercises the type surface the migration must handle: INT, NVARCHAR, DECIMAL(p,s),
DATETIME2, BIT (boolean-ish), NVARCHAR(MAX), VARBINARY(MAX), PK/FK constraints,
an index, and a view, plus rows with NULLs, an empty string (kept distinct from
NULL, like MySQL), unicode, and an embedded tab/newline.

Run:  python tests/fixtures/mssql_sample.py
"""
from __future__ import annotations

import datetime as dt

import pyodbc

HOST = "127.0.0.1"
PORT = 14433
USER = "sa"
PASSWORD = "Strong!Passw0rd"
DB = "hr"


def _conn_str(database: str) -> str:
    drivers = [d for d in pyodbc.drivers() if "SQL Server" in d]
    driver = next((d for d in drivers if "ODBC Driver" in d), drivers[0] if drivers else
                  "ODBC Driver 18 for SQL Server")
    return (
        "DRIVER={{{}}};SERVER={},{};DATABASE={};UID={};PWD={};"
        "TrustServerCertificate=yes;Encrypt=optional".format(
            driver, HOST, PORT, database, USER, PASSWORD))


def build() -> None:
    # Create the database on the master connection (CREATE DATABASE can't run in
    # a multi-statement batch / user txn).
    master = pyodbc.connect(_conn_str("master"), autocommit=True)
    mcur = master.cursor()
    mcur.execute(
        "IF DB_ID(N'{0}') IS NULL CREATE DATABASE [{0}]".format(DB))
    mcur.close()
    master.close()

    conn = pyodbc.connect(_conn_str(DB), autocommit=True)
    cur = conn.cursor()
    for stmt in (
        "IF OBJECT_ID('dbo.active_employees','V') IS NOT NULL DROP VIEW dbo.active_employees",
        "IF OBJECT_ID('dbo.employees','U') IS NOT NULL DROP TABLE dbo.employees",
        "IF OBJECT_ID('dbo.departments','U') IS NOT NULL DROP TABLE dbo.departments",
    ):
        cur.execute(stmt)

    cur.execute(
        "CREATE TABLE dbo.departments ("
        "  dept_id INT PRIMARY KEY,"
        "  dept_name NVARCHAR(50) NOT NULL)")
    cur.execute(
        "CREATE TABLE dbo.employees ("
        "  emp_id INT PRIMARY KEY,"
        "  full_name NVARCHAR(100) NOT NULL,"
        "  email NVARCHAR(120),"
        "  salary DECIMAL(10,2),"
        "  hired DATETIME2,"
        "  active BIT DEFAULT 1,"
        "  notes NVARCHAR(MAX),"
        "  photo VARBINARY(MAX),"
        "  dept_id INT,"
        "  CONSTRAINT emp_dept_fk FOREIGN KEY (dept_id) REFERENCES dbo.departments (dept_id))")
    cur.execute("CREATE INDEX emp_dept_idx ON dbo.employees (dept_id)")
    cur.execute(
        "CREATE VIEW dbo.active_employees AS "
        "SELECT emp_id, full_name, dept_id FROM dbo.employees WHERE active = 1")

    cur.executemany(
        "INSERT INTO dbo.departments (dept_id, dept_name) VALUES (?, ?)",
        [(10, "Engineering"), (20, "Sales"), (30, "Operations")])
    rows = [
        (1, "Ada Lovelace", "ada@example.com", 125000.50, dt.datetime(2019, 3, 1), 1,
         "Founding engineer", b"\x89PNG\x00\x01", 10),
        (2, "Alan Turing", None, None, dt.datetime(2020, 7, 15), 1, None, None, 10),
        (3, "Grace Hopper", "", 98000.00, dt.datetime(2018, 1, 20), 0,
         "Compiler\tpioneer\nmulti-line", None, 20),
        (4, "José Ñoño", "jose@example.com", 72000.25, dt.datetime(2021, 11, 5), 1,
         "Unicode name café", None, 30),
        (5, "Edsger Dijkstra", "ed@example.com", 110000.00, None, 1,
         "No hire date", None, None),
    ]
    cur.executemany(
        "INSERT INTO dbo.employees "
        "(emp_id, full_name, email, salary, hired, active, notes, photo, dept_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)

    cur.execute("SELECT COUNT(*) FROM dbo.departments")
    deps = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM dbo.employees")
    emps = cur.fetchone()[0]
    print("SQL Server sample ready: {} departments, {} employees".format(deps, emps))
    cur.close()
    conn.close()


if __name__ == "__main__":
    build()
