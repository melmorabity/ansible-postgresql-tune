#!/usr/bin/python
# -*- coding: utf-8 -*-

# Copyright © 2014-2018 Alexey Vasiliev (original pgtune project on
# https://github.com/le0pard/pgtune/, licensed under MIT license)
# Copyright © 2017-2018 Mohamed El Morabity (adaptation for Python and Ansible)
#
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU
# General Public License as published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without
# even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with this program. If not,
# see <http://www.gnu.org/licenses/>.


import datetime
from distutils.version import LooseVersion
import math
import os
import re
import tempfile

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.parsing.convert_bool import BOOLEANS


DOCUMENTATION = '''
---
module: postgresql_tune
author: Mohamed El Morabity
short_description: Tune PostgreSQL configuration by hardware.
description:
  - Optimize PostgreSQL configuration by setting up postgresql.conf parameter file according to hardware configuration and database usage. Tuning rules are based on https://github.com/le0pard/pgtune/, developed by Alexey Vasiliev and released under MIT license.
options:
  path:
    description:
      - Path to the PostgreSQL postgresql.conf to modify.
    type: path
    required: True
    aliases:
      - dest
      - destfile
      - name
  db_version:
    description:
      - PostgreSQL version.
    type: string
    required: False
    default: 9.6
  os_type:
    description:
      - Operation system type where PostgreSQL runs.
    type: string
    required: False
    default: linux
    choices:
      - linux
      - windows
  db_type:
    description:
      - Database usage type.
      - Available types are web applications (web), online transaction processing systems (oltp), data warehouses (dw), desktop applications (dw), mixed type of applications (mixed).
    type: string
    required: False
    default: mixed
    choices:
      - desktop
      - dw
      - mixed
      - oltp
      - web
  total_memory:
    description:
      - Total memory on the target PostgreSQL server, in megabytes.
    type: int
    required: True
  connections:
    description:
      - Maximum number of connections for PostgreSQL clients (minimum value is 10).
      - Will be computed automatically, dependency on the database type, if not specified.
    type: int
    required: False
  harddrive_type:
    description:
      - Hard drive type, which PostgreSQL use as storage for data.
    type: string
    required: False
    default: hdd
    choices:
      - hdd
      - ssd
      - san
  cpus:
    description:
      - Number of CPUs, which PostgreSQL can use.
    type: int
    required: False
    default: 1
  backup:
    description:
      - Create a backup file including the timestamp information so you can get the original file back if you somehow clobbered it incorrectly.
    type: bool
    required: False
    default: False
  follow:
    description:
      - This flag indicates that filesystem links, if they exist, should be followed.
    type: bool
    required: False
    default: False
  others:
    description:
      - All arguments accepted by the file module also work here.
    required: false
'''

EXAMPLES = '''
# Optimize PostgreSQL configuration
- local_action:
    module: postgresql_tune
    db_version: 9.2
    os_type: linux
    db_type: web
    total_memory: 8192
    path: /var/lib/pgsql/data/postgresql.conf
'''


def format_size(size):
    """Pretty-format a size in kB."""

    if size % (1 << 20) == 0:
        value = size >> 20
        unit = 'GB'
    elif size % (1 << 10) == 0:
        value = size >> 10
        unit = 'MB'
    else:
        value = size
        unit = 'kB'

    return '{}{}'.format(int(value), unit)


def get_pgtune_config(module):
    """Compute PostgreSQL optimizations."""

    db_version = module.params.get('db_version')
    os_type = module.params.get('os_type')
    db_type = module.params.get('db_type')
    total_memory = module.params.get('total_memory')
    connections = module.params.get('connections')
    harddrive_type = module.params.get('harddrive_type')
    cpus = module.params.get('cpus')

    config = {}
    warnings = []

    # Allow overriding the maximum connections
    if connections is None:
        if db_type == 'desktop':
            config['max_connections'] = 10
        elif db_type == 'dw':
            config['max_connections'] = 20
        elif db_type == 'mixed':
            config['max_connections'] = 100
        elif db_type == 'oltp':
            config['max_connections'] = 300
        elif db_type == 'web':
            config['max_connections'] = 200
    else:
        if connections < 10:
            module.fail_json(msg='connections must be an integer greater than or equal to 10')
        config['max_connections'] = connections

    if cpus <= 0 or cpus > 9999:
        module.fail_json(msg='cpus must be a strictly positive integer')

    total_memory_kb = total_memory << 10

    # This tool not being optimal for low memory systems
    if total_memory < 256:
        warnings.append('Not optimal for low memory systems')

    # This tool not being optimal for very high memory systems
    if total_memory > 100 << 10:
        warnings.append('Not optimal for very high memory systems')

    if db_type == 'desktop':
        config['shared_buffers'] = total_memory_kb / 16
    else:
        config['shared_buffers'] = total_memory_kb / 4

    # Limit shared_buffers to 512MB on Windows
    if os_type == 'windows' and config['shared_buffers'] > 512 << 10:
        config['shared_buffers'] = 512 << 10

    # Effective_cache_size
    if db_type == 'desktop':
        config['effective_cache_size'] = total_memory_kb / 4
    else:
        config['effective_cache_size'] = 3 * total_memory_kb / 4

    # work_mem is assigned any time a query calls for a sort, or a hash, or any other structure
    # that needs a space allocation, which can happen multiple times per query. So you're better
    # off assuming max_connections * 2 or max_connections * 3 is the amount of RAM that will
    # actually use in reality. At the very least, you need to subtract shared_buffers from the
    # amount you're distributing to connections in work_mem.
    # The other thing to consider is that there's no reason to run on the edge of available
    # memory. If you do that, there's a very high risk the out-of-memory killer will come along
    # and start killing PostgreSQL backends. Always leave a buffer of some kind in case of
    # spikes in memory usage. So your maximum amount of memory available in work_mem should be
    # ((RAM - shared_buffers) / (max_connections * 3) / max_parallel_workers_per_gather).
    max_parallel_workers_per_gather = int(math.ceil(0.5 * cpus))
    work_mem = (total_memory_kb - config['shared_buffers']) / \
               (3. * config['max_connections']) / max_parallel_workers_per_gather
    if db_type == 'desktop':
        config['work_mem'] = work_mem / 6.
    elif db_type == 'dw' or db_type == 'mixed':
        config['work_mem'] = work_mem / 2.
    else:
        config['work_mem'] = work_mem
    config['work_mem'] = int(math.floor(work_mem))
    if config['work_mem'] < 64:
        config['work_mem'] = 64

    # maintenance_work_mem
    if db_type == 'dw':
        config['maintenance_work_mem'] = total_memory_kb / 8
    else:
        config['maintenance_work_mem'] = total_memory_kb / 16
    # Cap maintenance RAM at 2GB on servers with lots of memory
    if config['maintenance_work_mem'] > 2 << 20:
        config['maintenance_work_mem'] = 2 << 20

    if LooseVersion(db_version) < LooseVersion('9.5'):
        # checkpoint_segments
        if db_type == 'desktop':
            config['checkpoint_segments'] = 3
        elif db_type == 'dw':
            config['checkpoint_segments'] = 128
        elif db_type == 'oltp':
            config['checkpoint_segments'] = 64
        else:
            config['checkpoint_segments'] = 32
    else:
        if db_type == 'desktop':
            config['min_wal_size'] = 100 << 10
            config['max_wal_size'] = 1024 << 10
        elif db_type == 'dw':
            config['min_wal_size'] = 4 << 20
            config['max_wal_size'] = 8 << 20
        elif db_type == 'oltp':
            config['min_wal_size'] = 2 << 20
            config['max_wal_size'] = 4 << 20
        else:
            config['min_wal_size'] = 1 << 20
            config['max_wal_size'] = 2 << 20

    # checkpoint_completion_target
    if db_type == 'desktop':
        config['checkpoint_completion_target'] = 0.5
    elif db_type == 'web':
        config['checkpoint_completion_target'] = 0.7
    else:
        config['checkpoint_completion_target'] = 0.9

    # wal_buffers
    # Follow auto-tuning guideline for wal_buffers added in 9.1, where it's set to 3% of
    # shared_buffers up to a maximum of 16MB
    if 'shared_buffers' in config:
        config['wal_buffers'] = 3 * config['shared_buffers'] / 100
        if config['wal_buffers'] > 16 << 10:
            config['wal_buffers'] = 16 << 10

        # It's nice of wal_buffers is an even 16MB if it's near that number. Since that is a common
        # case on Windows, where shared_buffers is clipped to 512MB, round upwards in that situation
        if 14 << 10 < config['wal_buffers'] < 16 << 10:
            config['wal_buffers'] = 16 << 10

    # default_statistics_target
    if db_type == 'dw':
        config['default_statistics_target'] = 500
    else:
        config['default_statistics_target'] = 100

    # hard drive type
    if harddrive_type == 'hdd':
        config['random_page_cost'] = 4
    else:
        config['random_page_cost'] = 1.1

    if os_type != 'windows':
        if harddrive_type == 'hdd':
            config['effective_io_concurrency'] = 2
        elif harddrive_type == 'ssd':
            config['effective_io_concurrency'] = 200
        elif harddrive_type == 'san':
            config['effective_io_concurrency'] = 300

    # CPU
    if LooseVersion(db_version) >= LooseVersion('9.5'):
        if cpus > 1:
            config['max_worker_processes'] = cpus
            if LooseVersion(db_version) >= LooseVersion('9.6'):
                config['max_parallel_workers_per_gather'] = max_parallel_workers_per_gather
                if LooseVersion(db_version) >= LooseVersion('10'):
                    config['max_parallel_workers'] = cpus

    # Format configuration
    for key, value in config.items():
        if not key in ['max_connections', 'checkpoint_segments', 'checkpoint_completion_target',
                       'default_statistics_target', 'random_page_cost',
                       'effective_io_concurrency', 'max_worker_processes',
                       'max_parallel_workers_per_gather', 'max_parallel_workers']:
            config[key] = format_size(value)

    return (config, warnings)


def write_changes(module, contents, path):
    """Write a string to a file, using a temporary file before to ensure changes are atomic."""

    tmp_fd, tmp_file = tempfile.mkstemp()
    tmp_object = os.fdopen(tmp_fd, 'wb')
    tmp_object.write(contents)
    tmp_object.close()

    module.atomic_move(tmp_file, path, unsafe_writes=module.params['unsafe_writes'])


def check_file_attrs(module, changed, message):
    """Check file attribute changes."""

    file_args = module.load_file_common_arguments(module.params)
    if module.set_file_attributes_if_different(file_args, False):
        if changed:
            message += ' and '
        changed = True
        message = 'ownership, perms or SE linux context changed'

    return (message, changed)


def write_optimizations(module):
    """Update input file with optimized PostgreSQL parameters."""

    path = module.params.get('path')
    backup = module.params.get('backup')
    follow = module.params.get('follow')

    if os.path.isdir(path):
        module.fail_json(rc=256, msg='Path {} is a directory!'.format(path))

    if not os.path.exists(path):
        module.fail_json(rc=257, msg='Path {} does not exist!'.format(path))

    pg_conf_object = open(path, 'rb')
    pg_conf_lines = pg_conf_object.readlines()
    pg_conf_object.close()

    pgtune_config, warnings = get_pgtune_config(module)

    results = {
        'pgtune': pgtune_config,
        'warnings': warnings
    }
    msg = ''

    if module._diff:
        results['diff'] = {'before_header': path, 'before': ''.join(pg_conf_lines),
                           'after_header': path}

    changed = False
    pgtune_config_to_append = dict(pgtune_config)
    updated_parameters = []
    added_parameters = []
    today = datetime.date.today()

    # Parse the file to update parameters in pgtune configuration
    for lineno, line in enumerate(pg_conf_lines):
        match = re.match(r'(?P<setup>[\s#]*(?P<key>[\S^#]+)\s*=\s*(?P<value>[\S^#]+)).*$', line)
        if match is None:
            continue

        key = match.group('key')
        value = match.group('value')
        if not key in pgtune_config:
            continue

        if str(pgtune_config[key]) == value:
            pgtune_config_to_append.pop(key)
            continue

        # Uncomment lines but keep in-line comments
        ansible_comment = '#Ansible: updated by postgresql_tune on {:%Y-%m-%d} ' \
                          '(previous value: {})'.format(today, value)
        pg_conf_lines[lineno] = '{}\n{}'.format(
            ansible_comment,
            line.replace(match.group('setup'), '{} = {}'.format(key, pgtune_config[key]))
        )
        changed = True
        pgtune_config_to_append.pop(key)
        updated_parameters.append(key)

    # Append parameters in pgtune configuration not present in the file
    for key, value in pgtune_config_to_append.items():
        ansible_comment = '#Ansible: added by postgresql_tune on {:%Y-%m-%d}'.format(today)
        pg_conf_lines.append('{}\n{} = {}\n'.format(ansible_comment, key, value))
        changed = True
        added_parameters.append(key)

    if updated_parameters:
        msg = 'parameters {} updated'.format(', '.join(updated_parameters))
    if added_parameters:
        msg += 'parameters {} added'.format(', '.join(added_parameters))

    contents = ''.join(pg_conf_lines)
    if module._diff:
        results['diff']['after'] = contents

    if changed:
        if backup and os.path.exists(path):
            results['backup_file'] = module.backup_local(path)
        if follow and os.path.islink(path):
            path = os.path.realpath(path)
        write_changes(module, contents, path)

    results['msg'], results['changed'] = check_file_attrs(module, changed, msg)

    return results


def main():
    """Main execution path."""

    module = AnsibleModule(
        argument_spec={
            'db_version': {'type': 'str', 'default': '9.6'},
            'os_type': {'type': 'str', 'choices': ['linux', 'windows'], 'default': 'linux'},
            'db_type': {'type': 'str', 'choices': ['desktop', 'dw', 'mixed', 'oltp', 'web'],
                        'default': 'mixed'},
            'total_memory': {'required': True, 'type': 'int'},
            'connections': {'type': 'int'},
            'harddrive_type': {'type': 'str', 'choices': ['hdd', 'ssd', 'san'], 'default': 'hdd'},
            'cpus': {'type': 'int', 'default': 1},
            'path': {'required': True, 'aliases': ['dest', 'destfile', 'name'], 'type': 'path'},
            'backup': {'type': 'bool', 'choices': BOOLEANS, 'default': False}
        },
        add_file_common_args=True,
        supports_check_mode=True
    )

    results = write_optimizations(module)

    module.exit_json(**results)


if __name__ == '__main__':
    main()
