"""Create a representative MySQL sample schema for integration testing.

Mirrors tests/fixtures/oracle_sample.py (same shape/data) so MySQL->HeliosDB can
be validated to Oracle parity. Idempotent. Targets a local MySQL:
    host=127.0.0.1 port=13306 user=root password=root db=hr

Note: unlike Oracle, MySQL keeps '' distinct from NULL, so the empty-string
email stays '' (not NULL) on this side.

Run:  python tests/fixtures/mysql_sample.py
"""
from __future__ import annotations

import datetime as dt

import pymysql

HOST = "127.0.0.1"
PORT = 13306
USER = "root"
PASSWORD = "root"
DB = "hr"


def build() -> None:
    conn = pymysql.connect(host=HOST, port=PORT, user=USER, password=PASSWORD, autocommit=True)
    cur = conn.cursor()
    cur.execute("CREATE DATABASE IF NOT EXISTS `{}` CHARACTER SET utf8mb4".format(DB))
    cur.execute("USE `{}`".format(DB))
    for stmt in (
        "DROP VIEW IF EXISTS active_employees",
        "DROP TABLE IF EXISTS employees",
        "DROP TABLE IF EXISTS departments",
    ):
        cur.execute(stmt)

    cur.execute(
        "CREATE TABLE departments ("
        "  dept_id INT PRIMARY KEY,"
        "  dept_name VARCHAR(50) NOT NULL)")
    cur.execute(
        "CREATE TABLE employees ("
        "  emp_id INT PRIMARY KEY,"
        "  full_name VARCHAR(100) NOT NULL,"
        "  email VARCHAR(120),"
        "  salary DECIMAL(10,2),"
        "  hired DATETIME,"
        "  active TINYINT(1) DEFAULT 1,"
        "  notes TEXT,"
        "  photo BLOB,"
        "  dept_id INT,"
        "  CONSTRAINT emp_dept_fk FOREIGN KEY (dept_id) REFERENCES departments (dept_id))")
    cur.execute("CREATE INDEX emp_dept_idx ON employees (dept_id)")
    cur.execute(
        "CREATE VIEW active_employees AS "
        "SELECT emp_id, full_name, dept_id FROM employees WHERE active = 1")

    cur.executemany(
        "INSERT INTO departments (dept_id, dept_name) VALUES (%s, %s)",
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
        "INSERT INTO employees "
        "(emp_id, full_name, email, salary, hired, active, notes, photo, dept_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)", rows)

    cur.execute("SELECT COUNT(*) FROM departments")
    deps = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM employees")
    emps = cur.fetchone()[0]
    print("MySQL sample ready: {} departments, {} employees".format(deps, emps))
    cur.close()
    conn.close()


if __name__ == "__main__":
    build()
