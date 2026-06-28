-- live application traffic on Pagila while CDC is running (zero downtime)
INSERT INTO actor (actor_id, first_name, last_name)
       VALUES (9001, 'Cdc', 'Newactor'), (9002, 'Cdc', 'Tobedeleted');
UPDATE actor SET last_name = 'Cdcupdated' WHERE actor_id = 1;
DELETE FROM actor WHERE actor_id = 9002;
