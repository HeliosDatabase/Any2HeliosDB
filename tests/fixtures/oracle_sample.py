"""Create a representative Oracle sample schema for integration testing.

Idempotent. Targets the local Oracle XE source container:
    user=hr password=hr dsn=localhost:1521/XEPDB1

Exercises the type/feature surface the migration must handle: NUMBER(p,s),
VARCHAR2, DATE, CLOB, BLOB, a boolean-ish NUMBER(1), PK/FK/CHECK constraints, a
sequence, a view, an index, and rows with NULLs, an empty string (Oracle folds
''→NULL), unicode, and embedded tab/newline.

Run:  python tests/fixtures/oracle_sample.py
"""
from __future__ import annotations

import datetime as dt

import oracledb

DSN = "localhost:1521/XEPDB1"
USER = "hr"
PASSWORD = "hr"


def _exec_ignore(cur, sql: str) -> None:
    try:
        cur.execute(sql)
    except oracledb.DatabaseError:
        pass


def build() -> None:
    conn = oracledb.connect(user=USER, password=PASSWORD, dsn=DSN)
    cur = conn.cursor()

    # Drop in dependency order (ignore "does not exist").
    for stmt in (
        "DROP VIEW active_employees",
        "DROP TABLE employees",
        "DROP TABLE departments",
        "DROP SEQUENCE emp_seq",
    ):
        _exec_ignore(cur, stmt)

    cur.execute(
        """
        CREATE TABLE departments (
            dept_id   NUMBER(6)    PRIMARY KEY,
            dept_name VARCHAR2(50) NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE employees (
            emp_id    NUMBER(10)    PRIMARY KEY,
            full_name VARCHAR2(100) NOT NULL,
            email     VARCHAR2(120),
            salary    NUMBER(10,2),
            hired     DATE,
            active    NUMBER(1)     DEFAULT 1,
            notes     CLOB,
            photo     BLOB,
            dept_id   NUMBER(6),
            CONSTRAINT emp_dept_fk FOREIGN KEY (dept_id) REFERENCES departments (dept_id),
            CONSTRAINT emp_salary_chk CHECK (salary >= 0)
        )
        """
    )
    cur.execute("CREATE SEQUENCE emp_seq START WITH 100 INCREMENT BY 1")
    cur.execute("CREATE INDEX emp_dept_idx ON employees (dept_id)")
    cur.execute(
        "CREATE VIEW active_employees AS "
        "SELECT emp_id, full_name, dept_id FROM employees WHERE active = 1"
    )

    cur.executemany(
        "INSERT INTO departments (dept_id, dept_name) VALUES (:1, :2)",
        [(10, "Engineering"), (20, "Sales"), (30, "Operations")],
    )

    rows = [
        (1, "Ada Lovelace", "ada@example.com", 125000.50, dt.datetime(2019, 3, 1), 1,
         "Founding engineer", b"\x89PNG\x00\x01", 10),
        (2, "Alan Turing", None, None, dt.datetime(2020, 7, 15), 1,
         None, None, 10),                         # NULL email/salary/notes/photo
        (3, "Grace Hopper", "", 98000.00, dt.datetime(2018, 1, 20), 0,
         "Compiler\tpioneer\nmulti-line", None, 20),  # '' email -> NULL; tab/newline in CLOB
        (4, "José Ñoño", "jose@example.com", 72000.25, dt.datetime(2021, 11, 5), 1,
         "Unicode name café", None, 30),
        (5, "Edsger Dijkstra", "ed@example.com", 110000.00, None, 1,
         "No hire date", None, None),             # NULL hired/dept
    ]
    cur.executemany(
        "INSERT INTO employees "
        "(emp_id, full_name, email, salary, hired, active, notes, photo, dept_id) "
        "VALUES (:1, :2, :3, :4, :5, :6, :7, :8, :9)",
        rows,
    )
    conn.commit()

    cur.execute("SELECT COUNT(*) FROM departments")
    deps = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM employees")
    emps = cur.fetchone()[0]
    print("Oracle sample ready: {} departments, {} employees".format(deps, emps))
    cur.close()
    conn.close()


if __name__ == "__main__":
    build()
