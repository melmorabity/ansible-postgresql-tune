# postgresql_tune

Tune PostgreSQL configuration by hardware.

## Synopsis

Optimize PostgreSQL configuration by setting up postgresql.conf parameter file according to hardware configuration and database usage. Optimizations are based on https://github.com/le0pard/pgtune/, developed by Alexey Vasiliev and released under MIT license.

## Options

| parameter      | required | default | choices                                                                     | comments                                                                                                                                                                                            |
| -------------- | -------- | ------- | --------------------------------------------------------------------------- |---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| db_version     | no       | 9.6     |                                                                             | PostgreSQL version.                                                                                                                                                                                 |
| total_memory   | yes      |         |                                                                             | Total memory on the target PostgreSQL server, in megabytes.                                                                                                                                         |
| harddrive_type | no       | hdd     | <ul><li>hdd</li><li>ssd</li><li>san</li></ul>                               | Hard drive type, which PostgreSQL use as storage for data.                                                                                                                                          |
| db_type        | no       | mixed   | <ul><li>desktop</li><li>dw</li><li>mixed</li><li>oltp</li><li>web</li></ul> | Database usage type. Available types are web applications (web), online transaction processing systems (oltp), data warehouses (dw), desktop applications (dw), mixed type of applications (mixed). |
| cpus           | no       | 1       |                                                                             | Number of CPUs, which PostgreSQL can use.                                                                                                                                                           |
| connections    | no       |         |                                                                             | Maximum number of connections for PostgreSQL clients (minimum value is 10). Will be computed automatically, dependency on the database type, if not specified.                                      |
| others         | no       |         |                                                                             | All arguments accepted by the file module also work here.                                                                                                                                           |
| follow         | no       | False   |                                                                             | This flag indicates that filesystem links, if they exist, should be followed.                                                                                                                       |
| path           | yes      |         |                                                                             | Path to the PostgreSQL postgresql.conf to modify.                                                                                                                                                   |
| backup         | no       | False   |                                                                             | Create a backup file including the timestamp information so you can get the original file back if you somehow clobbered it incorrectly.                                                             |
| os_type        | no       | linux   | <ul><li>linux</li><li>windows</li></ul>                                     | Operation system type where PostgreSQL runs.                                                                                                                                                        |

## Examples

```
# Optimize PostgreSQL configuration
- local_action:
    module: postgresql_tune
    db_version: 9.2
    os_type: linux
    db_type: web
    total_memory: 8192
    path: /var/lib/pgsql/data/postgresql.conf
```
