-- the app deletes a row (SCN watermark capture can't see deletes; reconcile does)
DELETE FROM employees WHERE emp_id = 6;
COMMIT;
