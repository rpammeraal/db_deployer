# db_deployer — Deploy database objects to PostgreSQL
# Copyright (C) 2026 Roy P. Ammeraal
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; version 2 of the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

import argparse
from os.path import basename
import os
import sys
import copy
import datetime
import time
import re

from .lib.db import db
from .lib import constants
from .lib.cache import cache
from .lib.sqlfile import sqlfile
from .lib.sqlpreprocessor import sqlpreprocessor

SUPPORTED_OBJECTS = [ 'role', 'database', 'schema', 'table', 'function', 'view', 'data', 'index', 'privilege' ]

def errorExit(error):
    print("An error has occurred: '{0}'".format(error))
    sys.exit(-1)


def process_database_change(db,file):
    pre_script = []
    #   Directory-based detection: filename (minus .sql) is the db name
    db_name = file.filename().replace('.sql','')

    if db.database_exists(db_name):
        print(f"\tDatabase {db_name} already exists -- skipping.")
        return pre_script

    contents = file.contents()
    contents = sqlpreprocessor.preprocess(contents)
    pre_script.extend(contents)
    return pre_script


def process_table_changes(db, file, verbose_flag=None):
    change_script = []

    #	add _tmp to table name
    org_table = file.object_name()
    tmp_table = file.object_name()

    if (tmp_table.find('.') != -1):
        tmp_table = 'tmp.' + tmp_table[tmp_table.find('.') + 1:]

    #	create table_tmp in tmp schema
    tmp_file = copy.deepcopy(file)
    tmp_file.set_object_name(tmp_table)

    drop_table_SQL = "DROP TABLE IF EXISTS " + tmp_file.object_name() + ' CASCADE;'
    SQL = []
    SQL.append(drop_table_SQL)
    SQL.extend(tmp_file.contents())

    if verbose_flag:
        print(f"process_table_changes:sql=\n{SQL}")

    result = db.execute(SQL, verbose_flag)
    if (result != None):
        errorExit(result)

    #	get field definition of org and tmp table
    org_def = {}
    tmp_def = {}
    org_def = db.object_definition(org_table)
    tmp_def = db.object_definition(tmp_table)

    all_fields = []
    all_fields = set(list(org_def.keys()) + list(tmp_def.keys()))

    #	Process difference
    for field in all_fields:
        if field in org_def and field not in tmp_def:
            print("\t\tRemoving column {0}".format(field))
            change_script.append("ALTER TABLE {0} DROP COLUMN {1};".format(
                org_table, field))

        if field not in org_def and field in tmp_def:
            print("\t\tAdding column {0}".format(field))

            default_clause = ''
            if (tmp_def[field].default() != None):
                default_clause = 'DEFAULT {0}'.format(tmp_def[field].default())

            not_null_clause = 'NULL'
            if (tmp_def[field].nullable_flag == 0):
                not_null_clause = 'NOT NULL'

            change_script.append(
                "ALTER TABLE {0} ADD COLUMN {1} {2} {3} {4};".format(
                    org_table, field, tmp_def[field].type(), not_null_clause,
                    default_clause))

    #	Examine changes in indexes
    org_table_index = {}
    new_table_index = {}

    org_table_index = db.all_index_definitions(org_table)
    new_table_index = db.all_index_definitions(tmp_table)

    all_indexes = []
    all_indexes = set(
        list(org_table_index.keys()) + list(new_table_index.keys()))

    for index in all_indexes:
        schema = org_table[:org_table.find('.')]
        if index in org_table_index and index not in new_table_index:
            change_script.append("DROP INDEX IF EXISTS {0}.{1};".format(schema, index))

        if index not in org_table_index and index in new_table_index:
            print("\t\tAdding index {0}".format(index))
            #	find index create statement in definition
            for l in file.contents():
                if (l.lower().find("create index") != -1
                        or l.lower().find("create unique index") != -1):
                    if l.lower().find(index) != -1:
                        change_script.append(l)

        if index in org_table_index and index in new_table_index:
            if (org_table_index[index] != new_table_index[index]):
                print("\t\tUpdating index {0}".format(index))
                change_script.append("DROP INDEX IF EXISTS {0}.{1};".format(
                    schema, index))
                for l in file.contents():
                    if (l.lower().find("create index") != -1
                            or l.lower().find("create unique index") != -1):
                        if l.lower().find(index) != -1:
                            change_script.append(l)

    #	Drop tmp table
    SQL = []
    SQL.append(drop_table_SQL)
    result = db.execute(SQL, 0)
    if (result != None):
        errorExit(result)

    return change_script


def process_index_changes(db, file):
    change_script = []

    #   Create list of indexes created
    for line in file.contents():
        if line.find('CREATE INDEX') != -1:
            index_name = line.split()[2]

            table = line.split()[4]
            schema = table.split('.')[0]

            change_script.append("DROP INDEX IF EXISTS {0}.{1};".format(schema, index_name))

    change_script.extend(file.contents())
    return change_script


def process_function_change(db, file):
    change_script = []

    #   Strip DEFAULT NULL from parameters
    function_header = file.object_name()
    pattern = re.compile(" DEFAULT NULL", re.IGNORECASE)
    function_header = pattern.sub("", function_header)

    change_script.append(
        "DROP FUNCTION IF EXISTS {0};".format(function_header))
    contents = []
    contents = file.contents()

    contents = sqlpreprocessor.preprocess(contents)
    change_script.extend(contents)

    return change_script


def process_view_change(db, file, dev_flag):
    change_script = []

    materialized_clause = '' if file.object_sub_type(
    ) == None else file.object_sub_type()

    change_script.append("DROP {0} VIEW IF EXISTS {1} CASCADE;".format(
        materialized_clause, file.object_name()))
    mat_view = ' '.join(file.contents())

    #   Strip optional semicolon
    if mat_view[-1:] == ';':
        mat_view = mat_view[:-1]

    change_script.append("-- {0}:{1}".format(file, dev_flag))
    if dev_flag == True:
        #   Add 'WITH NO DATA' so that materialized views are created quickly
        mat_view = mat_view + ' WITH NO DATA'

    #   Re-add semicolon
    mat_view = mat_view + ' ;'

    contents = []
    contents.append(mat_view)
    contents = sqlpreprocessor.preprocess(contents)
    change_script.extend(contents)

    return change_script


def process_data_change(db, file):
    change_script = []

    contents = []
    contents = file.contents()
    contents = sqlpreprocessor.preprocess(contents)
    change_script.extend(contents)

    return change_script


def process_role_change(db,file):
    change_script = []

    role_name = file.object_name()

    if not db.role_exists(role_name):
        contents = file.contents()
        contents = sqlpreprocessor.preprocess(contents)
        change_script.extend(contents)
    else:
        print(f'\tRole {role_name} already exists -- skipping.')

    return change_script


def get_items_to_be_refreshed(cache, path_list, dependencies, force_flag, items):
    for path in path_list:

        file_object = sqlfile(path)

        if cache.has_file_changed(file_object) == 1 or force_flag == 1:
            file_object_base_name = basename(path)

            found_items = []
            get_dependencies(dependencies, file_object_base_name, found_items)
            for f in found_items:
                if f not in items:
                    items.append(f)

    return


def get_dependencies(dependencies, object_file_name, found_items):

    if object_file_name not in found_items:
        found_items.append(object_file_name)

    for current_file in dependencies:
        if object_file_name in dependencies[current_file]:
            if current_file not in found_items:
                found_items.append(current_file)
                get_dependencies(dependencies, current_file, found_items)


def process_objects(db, cache, list, force_flag, dev_flag, verbose_flag):

    privilege_file = []
    change_script = []
    pre_script = []
    manifest = {}
    dependency = {}
    manifest_file_processed = []
    dependency_file_processed = []

    for type in SUPPORTED_OBJECTS:
        print("Processing {0}:".format(type))

        #	Read optional manifest(s)
        for path in sorted(list):
            if (type == list[path]):
                directory_path = os.path.dirname(os.path.realpath(path))
                manifest_path = directory_path + '/manifest.txt'

                if os.path.isfile(
                        manifest_path
                ) and manifest_path not in manifest_file_processed:
                    print("\tProcessing manifest at {0}".format(manifest_path))

                    line_number = 1
                    with open(manifest_path, 'r') as m:
                        for line in m:
                            line = line.strip('\n')
                            manifest[line] = line_number
                            line_number = line_number + 1

                    manifest_file_processed.append(
                        manifest_path
                    )  #	avoid reading the same file more than one

        #	Create ordered list
        ordered_list = {}
        unordered_list = []
        complete_list = []

        for path in sorted(list):
            if (type == list[path]):
                file_name = basename(path)
                if file_name in manifest:
                    ordered_list[manifest[file_name]] = path
                else:
                    unordered_list.append(path)

        for f in sorted(ordered_list):
            complete_list.append(ordered_list[f])
        for f in sorted(unordered_list):
            complete_list.append(f)

        #   Read (optional) dependencies:
        for path in sorted(list):
            if (type == list[path]):
                directory_path = os.path.dirname(os.path.realpath(path))
                dependency_path = directory_path + '/dependency.txt'

                if os.path.isfile(
                        dependency_path
                ) and dependency_path not in dependency_file_processed:
                    print("\tProcessing dependencies in {0}".format(dependency_path))

                    line_number = 1
                    with open(dependency_path, 'r') as m:
                        for line in m:
                            line = line.strip('\n')

                            line = line.strip()
                            if line:
                                (child, parent) = line.split(':', 2)
                                child=child.strip()
                                parent=parent.strip()

                                if child not in dependency.keys():
                                    dependency[child] = [ parent ]
                                else:
                                    dependency[child].append(parent)

                    dependency_file_processed.append(dependency_path)  #	avoid reading the same file more than one

                    #   Iterate through complete_list, removing dependencies until nothing is left
                    new_list=[]
                    num_deps_left=len(dependency) #   Not quite correct, we'll calculate after
                    while num_deps_left>0:
                        if verbose_flag:
                            print(f"\tDependencies:")
                            for d in dependency:
                                if dependency[d]:
                                    print(f"\t\t{d} -> {','.join(dependency[d])}")

                        prev_deps_left = num_deps_left

                        for f in complete_list:
                            fn=basename(f)

                            print(f"\tProcessing {fn}")
                            #   Find out if there is any dependency on this file.

                            if fn in dependency and len(dependency[fn])>=1:
                                #print(f"\t\t{dependency[fn]}:len={len(dependency[fn])}")
                                pass
                            else:
                                if f not in new_list:
                                    new_list.append(f)

                                    #   Remove fn from all list of dependents
                                    has_removals=False
                                    for de in dependency:
                                        for do in dependency[de]:
                                            if fn==do:
                                                dependency[de].remove(do)
                                                has_removals=True
                            
                        #   Calculate number of dependants correctly
                        num_deps_left=0
                        #print("\tDeps left:")
                        for f in dependency:
                            #print(f"\t\t{f}:{dependency[f]}:len={len(dependency[f])}")
                            num_deps_left+=len(dependency[f])

                        if num_deps_left == prev_deps_left:
                            #   No progress this iteration — remaining dependencies are unresolvable
                            print("\tERROR: unresolvable dependencies:")
                            for d in dependency:
                                if dependency[d]:
                                    print(f"\t\t{d} depends on {','.join(dependency[d])} which are not present")
                            sys.exit(-1)

                    #   Add missing items from complete_list to new_list (as their dependencies may have gone)
                    for i in complete_list:
                        if i not in new_list:
                            new_list.append(i)


                    #for x in complete_list:
                        #print(f"before {x}")
                    complete_list=new_list
                    #for x in complete_list:
                        #print(f"after  {x}")

                    items_to_be_refreshed = []
                    get_items_to_be_refreshed(cache, complete_list, dependency, force_flag, items_to_be_refreshed)
                    for f in items_to_be_refreshed:
                        cache.set_file_changed(f)

        for path in complete_list:

            #	read table definition in file
            file = sqlfile(path)

            if cache.has_file_changed(file) == 1 or force_flag == 1:

                if type == file.object_type():
                    print("\t" + file.object_name())
                    
                    #	if object name schema does not exist:

                    change_script.append("SELECT 'Processing {0} {1}';".format(
                        file.object_type(), file.object_name()))
                    if (file.object_type() == 'table'
                            or file.object_type() == 'schema'):
                        exists = db.object_exists(file.object_name(),file.object_type(),verbose_flag)
                        if (exists == 0):
                            #	create object
                            change_script.append(
                                "--\t{0} {1} does not exists -- create".format(
                                    (file.object_type()),
                                    (file.object_name())))
                            change_script.extend(file.contents())

                        else:
                            #	process changes -- table only, as there are no attributes to change for schemas
                            if (file.object_type() == 'table'):
                                change_script = change_script + process_table_changes(db,file,verbose_flag)

                    elif (file.object_type() == 'function'):
                        change_script = change_script + process_function_change(
                            db, file)

                    elif (file.object_type() in ['view', 'datacube']):
                        change_script = change_script + process_view_change(
                            db, file, dev_flag)

                    elif (file.object_type() == 'data'):
                        change_script = change_script + process_data_change(
                            db, file)

                    elif (file.object_type() == 'index'):
                        change_script = change_script + process_index_changes(
                            db, file)

                    elif (file.object_type() == 'role'):
                        change_script = change_script + process_role_change(
                            db, file)

                    elif (file.object_type() == 'privilege'):
                        privilege_file.append(file)

                    elif (file.object_type() == 'database'):
                        pre_script = pre_script + process_database_change(db,file)

                    cache.add_entry(file)

    return pre_script, change_script, privilege_file


def process_database_change(db,file):
    pre_script = []
    db_name = file.object_name()

    if db.database_exists(db_name):
        print(f"\tDatabase {db_name} already exists -- skipping.")
        return pre_script

    contents = file.contents()
    contents = sqlpreprocessor.preprocess(contents)
    pre_script.extend(contents)
    return pre_script


def store_change_script(database_name, change_script):
    file_name = "/tmp/deploy.{0}.{1}.sql".format(
        database_name,
        datetime.datetime.today().strftime('%Y%m%d-%H%m%S'))
    file = open(file_name, 'a')
    file.write("\n".join(change_script))
    file.write("\n")
    file.close()

    return file_name


def execute_privileges(current_db,privileges):

    print("Executing privileges:")
    for f in privileges:
        print("Processing privileges file {0}:".format(f.filename()))

        change_script = f.contents()
        change_script = sqlpreprocessor.preprocess(change_script)
        change_script.append('--   END OF PRIVILEGES\n')

        store_change_script(current_db.db_name(),change_script)
        result = current_db.execute(change_script,True)
        if result is not None:
            errorExit(result)

def process_files(repo_path, cache, files, database_name, force_flag, verbose_flag, dev_flag):

    db_path = repo_path + '/database/' + database_name

    objects = {}
    for name in files:
        if name.startswith('.'):
            continue

        p = name.split('/')

        #	Process database objects
        database_found = p[1]
        type = p[2].lower()
        if p[0] == 'database' and p[1].lower() == database_name.lower():
            if (type in SUPPORTED_OBJECTS):
                path = repo_path + "/" + name
                objects[path] = (type)
            else:
                print("type {0} NOT SUPPORTED YET!".format(type))
                sys.exit()

    #   Directory name = postgres database name
    current_db = db(database=database_name)

    #	create tmp schema if not exist
    #   Skip tmp schema for bootstrap databases (postgres, template1) — we only
    #   connect there to run CREATE ROLE / CREATE DATABASE, not for schema work.
    if database_name not in ('postgres','template1'):
        current_db.create_tmp_schema()
    print("Deploying on {0}@{1}:".format(current_db.db_name(),current_db.host()))

    change_script = []
    change_script.append("--	START OF CHANGESCRIPT on " + database_name)
    change_script.append("BEGIN;")

    ps,cs,pr=process_objects(current_db, cache, objects, force_flag, dev_flag, verbose_flag)

    #   Run pre-script (CREATE DATABASE and other non-transactional statements) FIRST,
    #   in autocommit, before opening the transaction for everything else.
    if len(ps) > 0:
        print("Executing pre-script (autocommit):")
        result = current_db.execute(ps,verbose_flag)
        if result is not None:
            errorExit(result)


    change_script.extend(cs)

    change_script.append("COMMIT;")
    change_script.append("--	END OF CHANGES")

    if (len(change_script) == 4):
        change_script = []

    if (len(change_script) != 0):

        file_name = store_change_script(database_name, change_script)
        print("Changescript available at: " + file_name)
        result = current_db.execute(change_script, verbose_flag)
        if (result != None):
            errorExit(result)

    cache.commit()

    execute_privileges(current_db,pr)

    current_db.close_db()

    return len(change_script)


def rebuild_cache(repo_root, cache, all_files):

    for path in all_files:
        path = repo_root + '/' + path  #   at this point we get relative paths.

        file_object = sqlfile(path)

        cache.add_entry(file_object)
    cache.commit()

    print("cache rebuild.")
    return


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]

    required_env = ['PGHOST','PGUSER']
    missing = [v for v in required_env if not os.environ.get(v)]
    if missing:
        print(f"Missing required environment variable(s): {', '.join(missing)}.")
        print("Set PGHOST and PGUSER (and optionally PGPORT, PGDATABASE), and configure ~/.pgpass for passwords.")
        sys.exit(-1)


    #	Set up parser
    parser = argparse.ArgumentParser(prog='db_deployer',description='Deploy changed or new files to your sandbox database')
    parser.add_argument('--repo',help="path to SQL repo (overrides $DB_DEPLOYER_REPO)")
    parser.add_argument('--db',help="comma separated list of databases")
    parser.add_argument('--dev',action="store_true",help="only deploys changed files")
    parser.add_argument('--rebuild_cache',action="store_true",help="rebuild cache")
    parser.add_argument('--run',action="store_true",help="run actual deployment")
    parser.add_argument('--verbose',action="store_true",help="show output")

    args = parser.parse_args()

    #  interpret arguments
    rebuild_cache_flag = args.rebuild_cache
    verbose_flag = args.verbose
    dev_flag = args.dev
    run_flag = args.run

    repo_path = args.repo if args.repo is not None else os.environ.get(constants._ENV_REPO)
    if repo_path is None:
        print(f"Repo path not set. Use --repo or export {constants._ENV_REPO}.")
        sys.exit(-1)

    i_did_something = False
    my_cache = cache(repo_path)

    main_db_path = repo_path + '/database'

    #   Collect databases in repo
    all_databases = {}
    for f_entry in os.listdir(main_db_path):
        db_path = main_db_path + '/' + f_entry
        if os.path.isdir(db_path):
            all_databases[f_entry] = db_path

    #   Narrow to specified db's in arguments
    db_to_process = {}
    if args.db != None:
        databases = args.db.split(",")
        for d in databases:
            if d in all_databases.keys():
                db_to_process[d] = all_databases[d]
            else:
                print(f"Unknown database {d}. Exiting.")
                sys.exit(-1)
    else:
        db_to_process = all_databases

    #   postgres must be processed first — it's where CREATE ROLE and CREATE DATABASE
    #   for the actual application databases live.
    db_order = list(db_to_process.keys())
    if 'postgres' in db_order:
        db_order.remove('postgres')
        db_order.insert(0,'postgres')

    for current_db in db_order:
        print("Collecting files for {0} database:".format(current_db))

        all_files = []
        db_path = db_to_process[current_db]

        for root, dirs, files in os.walk(db_path,topdown=False):
            for name in files:
                this_root = root[len(repo_path) + 1:]
                if (this_root.startswith('./')):
                    this_root = this_root[2:]

                filename = this_root + '/' + name
                if filename[:8] == 'database' and (
                            filename[len(filename) - 4:] == '.sql' or
                            filename[len(filename) - 3:] == '.ft'
                    ):
                    all_files.append(filename)

        if rebuild_cache_flag == True:
            rebuild_cache(repo_path,my_cache,all_files)
            return

        all_files.sort()

        if run_flag == True:
            process_files(repo_path,my_cache,all_files,current_db,0,verbose_flag,dev_flag)
            i_did_something = True

    if i_did_something == False:
        print("Dry-run complete. Add --run to deploy.\n")
        sys.exit(-1)

if __name__ == "__main__":
    sys.exit(main())
