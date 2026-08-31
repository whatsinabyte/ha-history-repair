-- ============================================================================
-- Safe on-device testing: an isolated database the add-on can reach, and a
-- user that cannot reach anything else.
-- ============================================================================
--
-- Run this ONCE against the MariaDB add-on on the ODROID, as a user with
-- privileges to create databases and users (usually the recorder user, which
-- the official MariaDB add-on grants ALL PRIVILEGES).
--
-- What it does, and why that makes on-device testing low risk:
--
--   * Creates a database `ha_test`, entirely separate from `homeassistant`.
--   * Creates a user `hc_test` whose grants cover `ha_test` and nothing else.
--
-- The add-on is then configured with those credentials. Even a catastrophic
-- bug in the add-on cannot modify, read, or drop the real recorder database,
-- because MariaDB refuses the connection's access at the server — this is a
-- permission boundary enforced by the database engine, not a promise made by
-- application code.
--
-- Change the password below before running. It only ever protects a
-- throwaway database, but it should still not be the word "changeme".
--
-- To remove everything afterwards:
--   DROP DATABASE ha_test;
--   DROP USER 'hc_test'@'%';
-- ============================================================================

CREATE DATABASE IF NOT EXISTS ha_test
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE USER IF NOT EXISTS 'hc_test'@'%' IDENTIFIED BY 'changeme';

-- Scoped to ha_test only. No grant on `homeassistant` is issued anywhere in
-- this script, and none should ever be added to it.
GRANT ALL PRIVILEGES ON ha_test.* TO 'hc_test'@'%';

FLUSH PRIVILEGES;

-- Verify the boundary. The first should list only ha_test grants; the second
-- must NOT include the recorder database.
SHOW GRANTS FOR 'hc_test'@'%';
