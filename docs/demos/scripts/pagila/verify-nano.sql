SELECT 'UPDATE  actor 1    -> last_name=' || last_name        FROM actor WHERE actor_id = 1;
SELECT 'INSERT  actor 9001 -> last_name=' || last_name        FROM actor WHERE actor_id = 9001;
SELECT 'DELETE  actor 9002 -> rows='      || count(*)::text   FROM actor WHERE actor_id = 9002;
