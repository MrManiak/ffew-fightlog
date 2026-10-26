CREATE USER 'aauth'@'%' IDENTIFIED BY 'authpass';
CREATE DATABASE alliance_auth CHARACTER SET utf8mb4 DEFAULT COLLATE utf8mb4_unicode_ci;
GRANT ALL PRIVILEGES ON alliance_auth.* TO 'aauth'@'%';
-- Django's test runner needs to create test_alliance_auth when tox runs in the container.
GRANT ALL PRIVILEGES ON test_alliance_auth.* TO 'aauth'@'%';
