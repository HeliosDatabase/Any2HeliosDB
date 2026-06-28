-- live application traffic on Oracle HR while CDC is running (zero downtime)
INSERT INTO employees (emp_id, full_name, email, salary, dept_id, active)
       VALUES (6, 'Linus Torvalds', 'linus@hr.example', 95000, 10, 1);
UPDATE employees SET salary = 130000 WHERE emp_id = 1;
COMMIT;
