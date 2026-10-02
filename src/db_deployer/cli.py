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

#	Environment variable naming the deployment environment (dev, prod, ...)
#	when --env is not given.
ENV_ENVIRONMENT = 'DB_DEPLOYER_ENV'

#	Object types whose directory may hold one subdirectory per environment:
#	data/dev/, data/prod/, ... Only the subdirectory matching the current
#	environment is deployed, next to the files in the directory itself.
ENVIRONMENT_OBJECTS = [ 'data' ]

SUPPORTED_OBJECTS = [ 'role', 'database', 'schema', 'table', 'function', 'procedure', 'view', 'data', 'index', 'privilege', 'post_deployment' ]

#	Object types that may share a directory: PostgreSQL routines are
#	interchangeable as far as the repository layout is concerned, so a
#	procedure may live in the function directory and vice versa. A datacube
#	is a view living in the datacube schema.
DIRECTORY_TYPE_ALIAS = {
    'function': ['function','procedure'],
    'procedure': ['procedure','function'],
    'view': ['view','datacube']
}


#	Matches an ALTER TABLE statement that adds a foreign key constraint.
FOREIGN_KEY_PATTERN = re.compile(r'^\s*ALTER\s+TABLE\b.*\bFOREIGN\s+KEY\b',re.IGNORECASE | re.DOTALL)


#	Returns True when an object type parsed from a file matches the type
#	implied by the directory the file lives in.
def type_matches_directory(directory_type, object_type):
    return object_type in DIRECTORY_TYPE_ALIAS.get(directory_type,[directory_type])


def errorExit(error):
    print("An error has occurred: '{0}'".format(error))
    sys.exit(-1)


#	Reduce an object identity to a comparable form: unquoted, single-spaced,
#	lower case. Both pg_identify_object() output and repository parsing go
#	through this, so quoting differences between the two do not matter.
def normalize_identity(identity):
    return re.sub(r'\s+',' ',identity.replace('"','')).strip().lower()


#	Reduce a definition to a comparable form: single-spaced. Definitions
#	printed by the catalog (pg_get_viewdef and friends) are identical for
#	identical objects, but bodies of PL/pgSQL routines are stored as written.
def normalize_sql(text):
    return re.sub(r'\s+',' ',text if text != None else '').strip()


#	Return the schema of 'schema.name' ('public' when unqualified).
def schema_of(name):
    return name.split('.')[0] if name.find('.') != -1 else 'public'


#	Return the 'tmp' schema twin of 'schema.name'.
def tmp_name_of(name):
    return 'tmp.' + name.split('.')[-1]


#	Return a definition printed by the catalog with every 'ON schema.name'
#	reduced to 'ON name', so that the definition of a tmp copy compares equal
#	to that of the real object.
def strip_on_schema(definition):
    return re.sub(r'\bON\s+(?:ONLY\s+)?(?:"[^"]*"|[^\s".]+)\.',' ON ',definition)


#	Drop tmp relation 'tmp.name' whatever its kind.
def drop_tmp_relation(db,tmp_relation):
    kind = db.relation_kind(tmp_relation)
    if kind == None:
        return None

    drop = { 'v': 'VIEW', 'm': 'MATERIALIZED VIEW' }.get(kind,'TABLE')
    return db.execute([ "DROP {0} IF EXISTS {1} CASCADE;".format(drop,tmp_relation) ],0)


#	Index of the objects defined in the repository, keyed by the kind and
#	identity that pg_identify_object() reports for them, so that an object
#	the database says depends on something can be traced back to the file
#	that defines it. Routines are keyed by name only -- overloads all match.
class repo_index:

    def __init__(self,list):
        self._entries = {}

        for path in sorted(list):
            type = list[path]
            file = sqlfile(path)

            if type == 'view':
                name = file.object_name()
                kind = 'materialized view' if file.object_sub_type() == 'materialized' else 'view'
                self._add(kind,name,path,type)

            elif type in ['function','procedure']:
                self._add(file.object_type(),file.object_name().split('(')[0],path,type)

            elif type in ['data','post_deployment']:
                for (policy,table) in file.policy_definitions():
                    self._add('policy',"{0} on {1}".format(policy,table),path,type)
                for (trigger,table) in file.trigger_definitions():
                    self._add('trigger',"{0} on {1}".format(trigger,table),path,type)

            if type in ['index','table']:
                #   An index lives in the schema of its table
                for (index,table) in file.index_definitions():
                    if index != '' and table.find('.') != -1:
                        self._add('index',table.split('.')[0] + '.' + index,path,type)


    def _add(self,kind,identity,path,type):
        key = (kind,normalize_identity(identity))

        if key not in self._entries:
            self._entries[key] = []

        self._entries[key].append((path,type))


    #	Return the [(path,type)] list of files defining the object, or [].
    def lookup(self,kind,identity):
        if kind in ['function','procedure']:
            identity = identity.split('(')[0]
            #   A procedure may live in the function directory and vice versa
            return self._entries.get(('function',normalize_identity(identity)),[]) + \
                   self._entries.get(('procedure',normalize_identity(identity)),[])

        return self._entries.get((kind,normalize_identity(identity)),[])


#	Prepare the removal of everything that depends on 'subject' (used in
#	messages only), so that the subject itself can be dropped without CASCADE.
#	'dependents' is what db.dependents() returned: (kind,identity) tuples in
#	drop order. Every one of them must be defined in the repository -- the run
#	is aborted otherwise, so that nothing outside the repository ever gets
#	dropped. Returns a (pre_script,post_script) pair: pre_script drops the
#	dependents and goes before the drop of the subject; post_script recreates
#	those that no later pass will take care of and goes after it. A dependent
#	whose object type is processed later in this run is flagged as changed
#	instead, and its own pass recreates it.
def resolve_dependents(cache,repo,current_type,subject,dependents,recreate_now=False,notes=None):
    pre_script = []
    post_script = []
    missing = []

    for (kind,identity) in dependents:
        definitions = repo.lookup(kind,identity)

        if len(definitions) == 0:
            missing.append("{0} {1}".format(kind,identity))
            continue

        pre_script.append("DROP {0} IF EXISTS {1};".format(kind.upper(),identity))

        for (path,type) in definitions:
            note = "dropping dependent {0} {1} -- recreated from {2}".format(kind,identity,basename(path))
            if notes != None:
                notes.append(note)
            else:
                print("\t\t" + note)

            if not recreate_now and SUPPORTED_OBJECTS.index(type) > SUPPORTED_OBJECTS.index(current_type):
                cache.set_file_changed(path)

            else:
                #   Its pass has already run (or is running), or there is no
                #   later pass at all: recreate it here.
                file = sqlfile(path)
                file.object_type()  #   strips OR REPLACE where applicable

                recreate = []

                if kind == 'index':
                    statement = find_index_statement(file,normalize_identity(identity).split('.')[-1])
                    if statement != None:
                        recreate.append(statement)

                elif kind in ['function','procedure']:
                    recreate.extend(sqlpreprocessor.preprocess(file.contents()))

                elif kind in ['view','materialized view']:
                    recreate.extend(sqlpreprocessor.preprocess(file.contents()))

                elif kind in ['policy','trigger']:
                    (name,table) = normalize_identity(identity).split(' on ',1)
                    for statement in file.contents():
                        header = sqlfile.parse_policy_header(statement) if kind == 'policy' else sqlfile.parse_trigger_header(statement)
                        if header != None and normalize_identity(header[0]) == name and normalize_identity(header[1]) == table:
                            recreate.append(statement)

                if len(recreate) == 0:
                    missing.append("{0} {1}".format(kind,identity))
                else:
                    #   Dependents are listed most-dependent first; they are
                    #   recreated in the opposite order.
                    post_script = recreate + post_script

    if len(missing) > 0:
        errorExit("cannot drop {0}, the following dependents are not defined in the repository:\n\t{1}".format(
            subject,"\n\t".join(missing)))

    return pre_script,post_script


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


#	Return the statement in 'file' that adds constraint 'constraint_name',
#	or None when the file does not contain such a statement (a constraint
#	declared inline with a column, say).
def find_constraint_statement(file, constraint_name):
    pattern = re.compile(r'^\s*ALTER\s+TABLE\s+\S+\s+ADD\s+CONSTRAINT\s+"?' + re.escape(constraint_name) + r'"?\s',re.IGNORECASE)
    for statement in file.contents():
        if pattern.match(statement) != None:
            return statement

    return None


#	Return the statement in 'file' that creates index 'index_name',
#	or None when the file does not contain such a statement.
def find_index_statement(file, index_name):
    for statement in file.contents():
        index_header = sqlfile.parse_index_header(statement)
        if index_header != None:
            if index_header[0].strip('"').lower() == index_name.strip('"').lower():
                return statement

    return None


#	Create the 'tmp' schema twin of the table in 'file'. Foreign keys are
#	left out unless asked for: the table they reference may be new in this
#	deployment, in which case its CREATE is still waiting in the change
#	script. Returns (tmp_table,error).
def create_tmp_table(db,file,keep_foreign_keys,verbose_flag):
    tmp_table = tmp_name_of(file.object_name())

    tmp_file = copy.deepcopy(file)
    tmp_file.set_object_name(tmp_table)

    result = drop_tmp_relation(db,tmp_table)
    if result != None:
        return (tmp_table,result)

    SQL = []
    for statement in tmp_file.contents():
        if keep_foreign_keys or FOREIGN_KEY_PATTERN.match(statement) == None:
            SQL.append(statement)

    if verbose_flag:
        print(f"create_tmp_table:sql=\n{SQL}")

    return (tmp_table,db.execute(SQL,verbose_flag))


#	Compare the table in 'file' with the database and return a
#	(differences,change_script) pair: one line per difference, and the
#	statements that resolve them. Without full_flag only columns and
#	indexes are compared (the deployment diff); with it also column types,
#	nullability, defaults and constraints (the --check diff).
def diff_table(db, cache, repo, file, full_flag, verbose_flag, recreate_now=False):
    differences = []
    change_script = []

    org_table = file.object_name()
    schema = schema_of(org_table)

    (tmp_table,result) = create_tmp_table(db,file,full_flag,verbose_flag)
    if result != None:
        if full_flag:
            differences.append("cannot create from repository: {0}".format(str(result).strip()))
            drop_tmp_relation(db,tmp_table)
            return (differences,change_script)
        errorExit(result)

    #	get field definition of org and tmp table
    org_def = db.object_definition(org_table)
    tmp_def = db.object_definition(tmp_table)

    #	Process difference
    for field in sorted(set(list(org_def.keys()) + list(tmp_def.keys()))):
        if field in org_def and field not in tmp_def:
            differences.append("column {0}: not in repository -- dropping".format(field))

            #   Whatever depends on the column goes first, and is recreated after
            (pre_script,post_script) = resolve_dependents(cache,repo,'table',
                "column {0}.{1}".format(org_table,field),
                db.column_dependents(org_table,field),recreate_now,differences)
            change_script.extend(pre_script)
            change_script.append("ALTER TABLE {0} DROP COLUMN {1};".format(
                org_table, field))
            change_script.extend(post_script)

        elif field not in org_def and field in tmp_def:
            differences.append("column {0}: missing in database -- adding".format(field))

            default = tmp_def[field].default()
            not_null = (tmp_def[field].nullable_flag() == 0)

            if default != None and re.search(r'\btmp\.',default) != None:
                #   A serial column: its default points at a sequence owned by
                #   the tmp table, which is about to be dropped.
                errorExit("column {0}.{1} has default {2}, which refers to the tmp schema. Create the sequence explicitly instead of using a serial type.".format(
                    org_table,field,default))

            default_clause = ''
            if default != None:
                default_clause = 'DEFAULT {0}'.format(default)

            if not_null and default != None:
                #   Add the column as nullable, fill whatever is still empty
                #   with the default, and only then tighten it.
                change_script.append(
                    "ALTER TABLE {0} ADD COLUMN {1} {2} NULL {3};".format(
                        org_table, field, tmp_def[field].raw_type(), default_clause))
                change_script.append(
                    "UPDATE {0} SET {1} = {2} WHERE {1} IS NULL;".format(
                        org_table, field, default))
                change_script.append(
                    "ALTER TABLE {0} ALTER COLUMN {1} SET NOT NULL;".format(
                        org_table, field))

            else:
                if not_null:
                    print("")
                    print("\t\t****************************************************************")
                    print("\t\t*  WARNING: column {0}.{1} is NOT NULL without a DEFAULT.".format(org_table,field))
                    print("\t\t*  Adding it fails when the table contains rows.")
                    print("\t\t****************************************************************")
                    print("")

                change_script.append(
                    "ALTER TABLE {0} ADD COLUMN {1} {2} {3} {4};".format(
                        org_table, field, tmp_def[field].raw_type(),
                        'NOT NULL' if not_null else 'NULL', default_clause))

        elif full_flag:
            org_field = org_def[field]
            tmp_field = tmp_def[field]

            if org_field.raw_type() != tmp_field.raw_type():
                differences.append("column {0}: database type {1}, repository {2}".format(field,org_field.raw_type(),tmp_field.raw_type()))
                change_script.append("ALTER TABLE {0} ALTER COLUMN {1} TYPE {2};".format(org_table,field,tmp_field.raw_type()))

            if org_field.nullable_flag() != tmp_field.nullable_flag():
                if tmp_field.nullable_flag() == 0:
                    differences.append("column {0}: database NULL, repository NOT NULL".format(field))
                    change_script.append("ALTER TABLE {0} ALTER COLUMN {1} SET NOT NULL;".format(org_table,field))
                else:
                    differences.append("column {0}: database NOT NULL, repository NULL".format(field))
                    change_script.append("ALTER TABLE {0} ALTER COLUMN {1} DROP NOT NULL;".format(org_table,field))

            #   A default owned by the tmp table (a sequence) refers to the tmp schema
            org_default = org_field.default()
            tmp_default = tmp_field.default()
            if tmp_default != None:
                tmp_default = re.sub(r'\btmp\.',schema + '.',tmp_default)

            if org_default != tmp_default:
                differences.append("column {0}: database default {1}, repository {2}".format(
                    field,org_default if org_default != None else 'none',tmp_default if tmp_default != None else 'none'))
                if tmp_default == None:
                    change_script.append("ALTER TABLE {0} ALTER COLUMN {1} DROP DEFAULT;".format(org_table,field))
                else:
                    change_script.append("ALTER TABLE {0} ALTER COLUMN {1} SET DEFAULT {2};".format(org_table,field,tmp_default))

    #	Examine changes in indexes (constraint-backed indexes are compared as constraints)
    org_table_index = db.all_index_definitions(org_table)
    new_table_index = db.all_index_definitions(tmp_table)

    for index in sorted(set(list(org_table_index.keys()) + list(new_table_index.keys()))):
        #   An index this table's file does not define, but another file does
        #   (the index directory), is that file's business.
        defined_elsewhere = False
        for (path,type) in repo.lookup('index',schema + '.' + index):
            if path != file.path():
                defined_elsewhere = True

        if defined_elsewhere:
            continue

        index_statement = find_index_statement(file,index)
        if index_statement == None and index in new_table_index:
            index_statement = new_table_index[index].replace(' ON tmp.',' ON ' + schema + '.',1) + ';'

        if index in org_table_index and index not in new_table_index:
            differences.append("index {0}: not in repository -- dropping".format(index))
            change_script.append("DROP INDEX IF EXISTS {0}.{1};".format(schema, index))

        elif index not in org_table_index and index in new_table_index:
            differences.append("index {0}: missing in database -- adding".format(index))
            change_script.append(index_statement)

        elif strip_on_schema(org_table_index[index]) != strip_on_schema(new_table_index[index]):
            differences.append("index {0}: differs -- recreating".format(index))
            change_script.append("DROP INDEX IF EXISTS {0}.{1};".format(schema, index))
            change_script.append(index_statement)

    #   Examine changes in constraints
    if full_flag:
        org_constraint = db.all_constraint_definitions(org_table)
        new_constraint = db.all_constraint_definitions(tmp_table)

        for constraint in sorted(set(list(org_constraint.keys()) + list(new_constraint.keys()))):
            #   Recreate from the file where possible: what the catalog prints
            #   does not always come back in the same internal form.
            constraint_statement = find_constraint_statement(file,constraint)
            if constraint_statement == None and constraint in new_constraint:
                constraint_statement = "ALTER TABLE {0} ADD CONSTRAINT {1} {2};".format(org_table,constraint,new_constraint[constraint][1])

            if constraint in org_constraint and constraint not in new_constraint:
                differences.append("constraint {0}: not in repository -- dropping".format(constraint))
                change_script.append("ALTER TABLE {0} DROP CONSTRAINT IF EXISTS {1};".format(org_table,constraint))

            elif constraint not in org_constraint and constraint in new_constraint:
                differences.append("constraint {0}: missing in database -- adding".format(constraint))
                change_script.append(constraint_statement)

            elif normalize_sql(org_constraint[constraint][1]) != normalize_sql(new_constraint[constraint][1]):
                differences.append("constraint {0}: differs -- recreating".format(constraint))
                change_script.append("ALTER TABLE {0} DROP CONSTRAINT IF EXISTS {1};".format(org_table,constraint))
                change_script.append(constraint_statement)

    #	Drop tmp table
    result = drop_tmp_relation(db,tmp_table)
    if (result != None):
        errorExit(result)

    return (differences,change_script)


def process_table_changes(db, cache, repo, file, verbose_flag=None):
    (differences,change_script) = diff_table(db,cache,repo,file,False,verbose_flag)

    for difference in differences:
        print("\t\t" + difference)

    return change_script


def process_index_changes(db, file):
    change_script = []

    #   Create list of indexes created
    for (index_name,table) in file.index_definitions():
        if index_name == '':
            #   Unnamed index -- the name is generated by the database, so there is nothing to drop
            continue

        #   An index lives in the schema of its table
        if table.find('.') != -1:
            change_script.append("DROP INDEX IF EXISTS {0}.{1};".format(table.split('.')[0],index_name))
        else:
            change_script.append("DROP INDEX IF EXISTS {0};".format(index_name))

    change_script.extend(file.contents())
    return change_script


#	Remove DEFAULT clauses (and their '=' shorthand) from a routine signature.
#	DROP FUNCTION and DROP PROCEDURE only accept argument modes, names and
#	types -- a default expression makes them fail.
def strip_argument_defaults(signature):
    open_position = signature.find('(')
    close_position = signature.rfind(')')

    if open_position == -1 or close_position < open_position:
        return signature

    arguments = []
    current = ''
    depth = 0

    for character in signature[open_position + 1:close_position]:
        if character in '([':
            depth = depth + 1
        elif character in ')]':
            depth = depth - 1

        if character == ',' and depth == 0:
            arguments.append(current)
            current = ''
        else:
            current = current + character

    arguments.append(current)

    stripped = []
    for argument in arguments:
        default_position = -1

        match = re.search(r'\s+DEFAULT\s',argument,re.IGNORECASE)
        if match != None:
            default_position = match.start()
        else:
            #   '=' is the shorthand for DEFAULT, and only appears at depth 0
            depth = 0
            for position,character in enumerate(argument):
                if character in '([':
                    depth = depth + 1
                elif character in ')]':
                    depth = depth - 1
                elif character == '=' and depth == 0:
                    default_position = position
                    break

        if default_position != -1:
            argument = argument[:default_position]

        stripped.append(argument.strip())

    return "{0}({1}){2}".format(signature[:open_position],', '.join(stripped),signature[close_position + 1:])


def process_function_change(db, cache, repo, file, recreate_now=False, notes=None):
    change_script = []

    #   Strip DEFAULT clauses from parameters
    function_header = strip_argument_defaults(file.object_name())

    #   Whatever depends on the function goes first, and is recreated after
    (pre_script,post_script) = resolve_dependents(cache,repo,'function',
        "function {0}".format(function_header),
        db.routine_dependents(function_header),recreate_now,notes)
    change_script.extend(pre_script)

    change_script.append(
        "DROP FUNCTION IF EXISTS {0};".format(function_header))
    contents = []
    contents = file.contents()

    contents = sqlpreprocessor.preprocess(contents)
    change_script.extend(contents)
    change_script.extend(post_script)

    return change_script


def process_procedure_change(db, cache, repo, file, recreate_now=False, notes=None):
    change_script = []

    #   Strip DEFAULT clauses from parameters
    procedure_header = strip_argument_defaults(file.object_name())

    #   Whatever depends on the procedure goes first, and is recreated after
    (pre_script,post_script) = resolve_dependents(cache,repo,'procedure',
        "procedure {0}".format(procedure_header),
        db.routine_dependents(procedure_header),recreate_now,notes)
    change_script.extend(pre_script)

    change_script.append(
        "DROP PROCEDURE IF EXISTS {0};".format(procedure_header))
    contents = []
    contents = file.contents()

    contents = sqlpreprocessor.preprocess(contents)
    change_script.extend(contents)
    change_script.extend(post_script)

    return change_script


#	Matches the name in a routine creation statement (OR REPLACE already stripped).
ROUTINE_HEADER_PATTERN = re.compile(r'^(\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:FUNCTION|PROCEDURE)\s+)[^\s(]+',re.IGNORECASE)


#	Compare the routine in 'file' with the database by creating it in the
#	tmp schema and comparing what the catalog prints for both. Only the
#	first statement of the file (the CREATE) is used for the copy. Returns
#	a (differences,change_script) pair.
def diff_routine(db, cache, repo, file, verbose_flag):
    differences = []
    change_script = []

    kind = file.object_type()  #   function or procedure; strips OR REPLACE
    signature = file.object_name()
    name = signature.split('(')[0].strip()
    tmp_name = tmp_name_of(name)

    def drop_statement(routine_name,prokind,arguments):
        return "DROP {0} IF EXISTS {1}({2});".format('PROCEDURE' if prokind == 'p' else 'FUNCTION',routine_name,arguments)

    #   Start from a clean tmp slate, then create the copy
    SQL = []
    for (oid,prokind,arguments) in db.routine_signatures(tmp_name):
        SQL.append(drop_statement(tmp_name,prokind,arguments))

    SQL.append(ROUTINE_HEADER_PATTERN.sub(lambda m: m.group(1) + tmp_name,file.contents()[0],count=1))

    result = db.execute(SQL,verbose_flag)
    if result != None:
        differences.append("cannot create from repository: {0}".format(str(result).strip()))
        return (differences,change_script)

    tmp_signatures = db.routine_signatures(tmp_name)
    (tmp_oid,tmp_kind,tmp_arguments) = tmp_signatures[0]

    org_signatures = [ s for s in db.routine_signatures(name) if s[1] == tmp_kind and s[2] == tmp_arguments ]

    if len(org_signatures) == 0:
        differences.append("missing in database")
    else:
        org_definition = db.routine_definition(org_signatures[0][0])
        tmp_definition = db.routine_definition(tmp_oid).replace(tmp_name + '(',name + '(',1)

        if normalize_sql(org_definition) != normalize_sql(tmp_definition):
            differences.append("definition differs")

    db.execute([ drop_statement(tmp_name,tmp_kind,tmp_arguments) ],0)

    if len(differences) > 0:
        if kind == 'procedure':
            change_script = process_procedure_change(db,cache,repo,file,True,differences)
        else:
            change_script = process_function_change(db,cache,repo,file,True,differences)

    return (differences,change_script)


#	Compare the view in 'file' with the database by creating it in the tmp
#	schema and comparing what the catalog prints for both. Returns a
#	(differences,change_script) pair.
def diff_view(db, cache, repo, file, dev_flag, verbose_flag):
    differences = []
    change_script = []

    file.object_type()  #   strips OR REPLACE
    name = file.object_name()
    tmp_name = tmp_name_of(name)
    materialized = (file.object_sub_type() == 'materialized')

    tmp_file = copy.deepcopy(file)
    tmp_file.set_object_name(tmp_name)

    result = drop_tmp_relation(db,tmp_name)
    if result == None:
        statement = ' '.join(tmp_file.contents()).strip()
        if statement[-1:] == ';':
            statement = statement[:-1]
        if materialized:
            statement = statement + ' WITH NO DATA'
        result = db.execute([ statement + ';' ],verbose_flag)

    if result != None:
        differences.append("cannot create from repository: {0}".format(str(result).strip()))
        drop_tmp_relation(db,tmp_name)
        return (differences,change_script)

    org_kind = db.relation_kind(name)
    expected_kind = 'm' if materialized else 'v'

    if org_kind == None:
        differences.append("missing in database")
    elif org_kind != expected_kind:
        differences.append("database has a {0}, repository a {1}".format(
            'materialized view' if org_kind == 'm' else 'view' if org_kind == 'v' else 'relation of kind ' + org_kind,
            'materialized view' if materialized else 'view'))
    elif normalize_sql(db.view_definition(name)) != normalize_sql(db.view_definition(tmp_name)):
        differences.append("definition differs")

    drop_tmp_relation(db,tmp_name)

    if len(differences) > 0:
        change_script = process_view_change(db,cache,repo,file,dev_flag,True,differences)

    return (differences,change_script)


def process_view_change(db, cache, repo, file, dev_flag, recreate_now=False, notes=None):
    change_script = []

    materialized_clause = '' if file.object_sub_type(
    ) == None else file.object_sub_type().upper()

    #   Whatever depends on the view goes first, and is recreated after
    (pre_script,post_script) = resolve_dependents(cache,repo,'view',
        "view {0}".format(file.object_name()),
        db.relation_dependents(file.object_name()),recreate_now,notes)
    change_script.extend(pre_script)

    change_script.append("DROP {0} VIEW IF EXISTS {1} CASCADE;".format(
        materialized_clause, file.object_name()))
    mat_view = ' '.join(file.contents())

    #   Strip optional semicolon
    if mat_view[-1:] == ';':
        mat_view = mat_view[:-1]

    change_script.append("-- {0}".format(file.filename()))
    if dev_flag == True:
        #   Add 'WITH NO DATA' so that materialized views are created quickly
        mat_view = mat_view + ' WITH NO DATA'

    #   Re-add semicolon
    mat_view = mat_view + ' ;'

    contents = []
    contents.append(mat_view)
    contents = sqlpreprocessor.preprocess(contents)
    change_script.extend(contents)
    change_script.extend(post_script)

    return change_script


#	Compare the indexes in an index directory file with the database, by
#	creating them on a tmp copy of their table. Returns a
#	(differences,change_script) pair.
def diff_index_file(db, cache, repo, file, tables, verbose_flag):
    differences = []
    change_script = []

    for (index,table) in file.index_definitions():
        if index == '':
            differences.append("unnamed index on {0}: cannot be checked".format(table))
            continue

        table_key = normalize_identity(table)
        if table_key not in tables:
            differences.append("index {0}: table {1} not in repository".format(index,table))
            continue

        table_file = sqlfile(tables[table_key])
        org_table = table_file.object_name()
        schema = schema_of(org_table)

        (tmp_table,result) = create_tmp_table(db,table_file,False,verbose_flag)
        if result == None:
            statement = find_index_statement(file,index)
            result = db.execute([ re.sub(r'\bON\s+(?:ONLY\s+)?' + re.escape(table) + r'\b','ON ' + tmp_table,statement,count=1,flags=re.IGNORECASE) ],verbose_flag)

        if result != None:
            differences.append("index {0}: cannot create from repository: {1}".format(index,str(result).strip()))
            drop_tmp_relation(db,tmp_table)
            continue

        org_index = db.all_index_definitions(org_table)
        tmp_index = db.all_index_definitions(tmp_table)

        if index not in org_index:
            differences.append("index {0}: missing in database -- adding".format(index))
            change_script.append(find_index_statement(file,index))
        elif strip_on_schema(org_index[index]) != strip_on_schema(tmp_index[index]):
            differences.append("index {0}: differs -- recreating".format(index))
            change_script.append("DROP INDEX IF EXISTS {0}.{1};".format(schema,index))
            change_script.append(find_index_statement(file,index))

        drop_tmp_relation(db,tmp_table)

    return (differences,change_script)


#	Return {normalized table name: path} for the table files in 'objects'.
def table_files(objects):
    tables = {}
    for path in sorted(objects):
        if objects[path] == 'table':
            tables[normalize_identity(sqlfile(path).object_name())] = path

    return tables


#	Compare the policies and triggers the data and post_deployment files
#	define with the database. They are created on a tmp copy of their
#	table and compared through the catalog. Returns a list of
#	(subject,differences,change_script) tuples.
def diff_policies_and_triggers(db, cache, repo, objects, verbose_flag):
    results = []
    num_checked = 0

    tables = table_files(objects)

    #   Policies and triggers by normalized table name: [(kind,name,statement)]
    wanted = {}
    for path in sorted(objects):
        if objects[path] in ['data','post_deployment']:
            for statement in sqlfile(path).contents():
                header = sqlfile.parse_policy_header(statement)
                if header != None:
                    wanted.setdefault(normalize_identity(header[1]),[]).append(('policy',header[0],statement))
                header = sqlfile.parse_trigger_header(statement)
                if header != None:
                    wanted.setdefault(normalize_identity(header[1]),[]).append(('trigger',header[0],statement))

    for table in sorted(set(list(tables.keys()) + list(wanted.keys()))):
        if table not in tables:
            for (kind,name,statement) in wanted[table]:
                results.append(("{0} {1} on {2}".format(kind,name,table),[ "table not in repository" ],[]))
            continue

        table_file = sqlfile(tables[table])
        org_table = table_file.object_name()

        (tmp_table,result) = create_tmp_table(db,table_file,False,verbose_flag)
        if result == None:
            SQL = []
            for (kind,name,statement) in wanted.get(table,[]):
                if kind == 'policy':
                    SQL.append(sqlfile.rewrite_policy_table(statement,tmp_table))
                else:
                    SQL.append(sqlfile.rewrite_trigger_table(statement,tmp_table))
            result = db.execute(SQL,verbose_flag)

        if result != None:
            results.append(("policies and triggers on {0}".format(org_table),[ "cannot create from repository: {0}".format(str(result).strip()) ],[]))
            drop_tmp_relation(db,tmp_table)
            continue

        statements = {}
        for (kind,name,statement) in wanted.get(table,[]):
            statements[(kind,normalize_identity(name))] = statement
            num_checked = num_checked + 1

        org_policy = db.policy_definitions(org_table)
        tmp_policy = db.policy_definitions(tmp_table)
        org_trigger = db.trigger_definitions(org_table)
        tmp_trigger = db.trigger_definitions(tmp_table)

        for kind,org,tmp in [ ('policy',org_policy,tmp_policy),('trigger',org_trigger,tmp_trigger) ]:
            for name in sorted(set(list(org.keys()) + list(tmp.keys()))):
                subject = "{0} {1} on {2}".format(kind,name,org_table)
                drop = "DROP {0} IF EXISTS {1} ON {2};".format(kind.upper(),name,org_table)
                statement = statements.get((kind,normalize_identity(name)))

                if name in tmp and name not in org:
                    results.append((subject,[ "missing in database -- adding" ],[ drop,statement ]))
                elif name in org and name not in tmp:
                    results.append((subject,[ "not in repository -- dropping" ],[ drop ]))
                else:
                    if kind == 'policy':
                        same = (org[name] == tmp[name])
                    else:
                        same = (strip_on_schema(org[name]) == strip_on_schema(tmp[name]))
                    if not same:
                        results.append((subject,[ "differs -- recreating" ],[ drop,statement ]))

        drop_tmp_relation(db,tmp_table)

    return (num_checked,results)


def process_data_change(db, file):
    change_script = []

    #   A policy cannot be created with OR REPLACE, so drop it first to make
    #   the file re-runnable. The same for triggers, as not every PostgreSQL
    #   version accepts OR REPLACE there. Everything else is passed through
    #   untouched.
    contents = []
    for statement in file.contents():
        policy_header = sqlfile.parse_policy_header(statement)
        if policy_header != None:
            contents.append("DROP POLICY IF EXISTS {0} ON {1};".format(policy_header[0],policy_header[1]))

        trigger_header = sqlfile.parse_trigger_header(statement)
        if trigger_header != None:
            contents.append("DROP TRIGGER IF EXISTS {0} ON {1};".format(trigger_header[0],trigger_header[1]))

        contents.append(statement)

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


#	Return the files of 'type' in 'list' in deployment order: manifest.txt
#	order first, then dependency.txt order, then alphabetical. 'state' keeps
#	the manifests and dependencies read so far across calls. With a cache,
#	files depending on a changed file are flagged as changed too.
def order_paths(list, type, state, verbose_flag, cache=None, force_flag=0):
    #	Read optional manifest(s)
    for path in sorted(list):
        if (type == list[path]):
            directory_path = os.path.dirname(os.path.realpath(path))
            manifest_path = directory_path + '/manifest.txt'

            if os.path.isfile(
                    manifest_path
            ) and manifest_path not in state['manifest_file_processed']:
                print("\tProcessing manifest at {0}".format(manifest_path))

                line_number = 1
                with open(manifest_path, 'r') as m:
                    for line in m:
                        line = line.strip('\n')
                        state['manifest'][line] = line_number
                        line_number = line_number + 1

                state['manifest_file_processed'].append(
                    manifest_path
                )  #	avoid reading the same file more than one

    #	Create ordered list
    ordered_list = {}
    unordered_list = []
    complete_list = []

    for path in sorted(list):
        if (type == list[path]):
            file_name = basename(path)
            if file_name in state['manifest']:
                ordered_list[state['manifest'][file_name]] = path
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
            ) and dependency_path not in state['dependency_file_processed']:
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

                            if child not in state['dependency'].keys():
                                state['dependency'][child] = [ parent ]
                            else:
                                state['dependency'][child].append(parent)

                state['dependency_file_processed'].append(dependency_path)  #	avoid reading the same file more than one

                #   Iterate through complete_list, removing dependencies until nothing is left
                new_list=[]
                num_deps_left=len(state['dependency']) #   Not quite correct, we'll calculate after
                while num_deps_left>0:
                    if verbose_flag:
                        print(f"\tDependencies:")
                        for d in state['dependency']:
                            if state['dependency'][d]:
                                print(f"\t\t{d} -> {','.join(state['dependency'][d])}")

                    prev_deps_left = num_deps_left

                    for f in complete_list:
                        fn=basename(f)

                        print(f"\tProcessing {fn}")
                        #   Find out if there is any dependency on this file.

                        if fn in state['dependency'] and len(state['dependency'][fn])>=1:
                            #print(f"\t\t{state['dependency'][fn]}:len={len(state['dependency'][fn])}")
                            pass
                        else:
                            if f not in new_list:
                                new_list.append(f)

                                #   Remove fn from all list of dependents
                                has_removals=False
                                for de in state['dependency']:
                                    for do in state['dependency'][de]:
                                        if fn==do:
                                            state['dependency'][de].remove(do)
                                            has_removals=True
                        
                    #   Calculate number of dependants correctly
                    num_deps_left=0
                    #print("\tDeps left:")
                    for f in state['dependency']:
                        #print(f"\t\t{f}:{state['dependency'][f]}:len={len(state['dependency'][f])}")
                        num_deps_left+=len(state['dependency'][f])

                    if num_deps_left == prev_deps_left:
                        #   No progress this iteration — remaining dependencies are unresolvable
                        print("\tERROR: unresolvable dependencies:")
                        for d in state['dependency']:
                            if state['dependency'][d]:
                                print(f"\t\t{d} depends on {','.join(state['dependency'][d])} which are not present")
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

                if cache != None:
                    items_to_be_refreshed = []
                    get_items_to_be_refreshed(cache, complete_list, state['dependency'], force_flag, items_to_be_refreshed)
                    for f in items_to_be_refreshed:
                        cache.set_file_changed(f)

    return complete_list


def ordering_state():
    return { 'manifest': {}, 'dependency': {}, 'manifest_file_processed': [], 'dependency_file_processed': [] }


def process_objects(db, cache, list, force_flag, dev_flag, verbose_flag):

    privilege_file = []
    change_script = []
    pre_script = []
    post_script = []
    state = ordering_state()

    #   Everything the repository defines, for tracing database dependents back to their files
    repo = repo_index(list)

    for type in SUPPORTED_OBJECTS:
        print("Processing {0}:".format(type))

        complete_list = order_paths(list,type,state,verbose_flag,cache,force_flag)

        for path in complete_list:

            #	read table definition in file
            file = sqlfile(path)

            if cache.has_file_changed(file) == 1 or force_flag == 1:

                if type_matches_directory(type, file.object_type()):
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
                                change_script = change_script + process_table_changes(db,cache,repo,file,verbose_flag)

                    elif (file.object_type() == 'function'):
                        change_script = change_script + process_function_change(
                            db, cache, repo, file)

                    elif (file.object_type() == 'procedure'):
                        change_script = change_script + process_procedure_change(
                            db, cache, repo, file)

                    elif (file.object_type() in ['view', 'datacube']):
                        change_script = change_script + process_view_change(
                            db, cache, repo, file, dev_flag)

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

                    elif (file.object_type() == 'post_deployment'):
                        post_script = post_script + process_data_change(
                            db, file)

                    cache.add_entry(file)

                else:
                    #   A file whose contents do not match its directory would be
                    #   silently skipped otherwise -- abort instead.
                    errorExit("{0} is in the {1} directory but contains a {2} definition".format(
                        path, type, file.object_type()))

    return pre_script, change_script, privilege_file, post_script


#	Compare every table, view, function and procedure in 'list', and the
#	policies and triggers in its data files, with the database. Prints the
#	differences and returns (objects_checked,differences,fix_script).
def check_objects(db, cache, objects, dev_flag, verbose_flag):
    num_objects = 0
    num_differences = 0
    fix_script = []
    state = ordering_state()

    repo = repo_index(objects)

    def report(subject,differences,change_script):
        nonlocal num_differences
        if len(differences) == 0:
            if verbose_flag:
                print("\t{0}: OK".format(subject))
            return
        print("\t{0}".format(subject))
        for difference in differences:
            print("\t\t{0}".format(difference))
        num_differences = num_differences + len(differences)
        if len(change_script) > 0:
            fix_script.append("SELECT 'Fixing {0}';".format(subject))
            fix_script.extend(change_script)

    tables = table_files(objects)

    for type in ['table','view','function','procedure','index']:
        print("Checking {0}:".format(type))

        for path in order_paths(objects,type,state,verbose_flag):
            file = sqlfile(path)

            if not type_matches_directory(type,file.object_type()):
                errorExit("{0} is in the {1} directory but contains a {2} definition".format(path,type,file.object_type()))

            num_objects = num_objects + 1

            if type == 'table':
                if db.object_exists(file.object_name(),'table',verbose_flag) == 0:
                    (differences,change_script) = ([ "missing in database -- creating" ],sqlpreprocessor.preprocess(file.contents()))
                else:
                    (differences,change_script) = diff_table(db,cache,repo,file,True,verbose_flag,True)

            elif type == 'view':
                (differences,change_script) = diff_view(db,cache,repo,file,dev_flag,verbose_flag)

            elif type == 'index':
                (differences,change_script) = diff_index_file(db,cache,repo,file,tables,verbose_flag)

            else:
                (differences,change_script) = diff_routine(db,cache,repo,file,verbose_flag)

            subject = "{0} {1}".format(file.object_type(),file.object_name())
            if type == 'index':
                subject = "index file {0}".format(file.filename())
            report(subject,differences,change_script)

    print("Checking policy and trigger:")
    (num_checked,results) = diff_policies_and_triggers(db,cache,repo,objects,verbose_flag)
    num_objects = num_objects + num_checked
    for (subject,differences,change_script) in results:
        report(subject,differences,change_script)

    return (num_objects,num_differences,fix_script)


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

def execute_post_deployment(current_db,post_script,verbose_flag):

    if len(post_script) == 0:
        return

    print("Executing post-deployment:")

    change_script = []
    change_script.append("--\tSTART OF POST-DEPLOYMENT on " + current_db.db_name())
    change_script.append("BEGIN;")
    change_script.extend(post_script)
    change_script.append("COMMIT;")
    change_script.append("--\tEND OF POST-DEPLOYMENT")

    file_name = store_change_script(current_db.db_name(),change_script)
    print("Post-deployment script available at: " + file_name)
    result = current_db.execute(change_script,verbose_flag)
    if result is not None:
        errorExit(result)


#	Return {path: type} for the files of 'database_name' in 'files', leaving
#	out environment directories other than 'environment'.
def collect_objects(repo_path, files, database_name, environment):
    objects = {}
    skipped_environments = []
    for name in files:
        if name.startswith('.'):
            continue

        p = name.split('/')

        #	Process database objects
        database_found = p[1]
        type = p[2].lower()
        if p[0] == 'database' and p[1].lower() == database_name.lower():
            if (type in SUPPORTED_OBJECTS):
                if type in ENVIRONMENT_OBJECTS and len(p) > 4:
                    #   <type>/<environment>/...: only the current environment's files
                    environment_found = p[3]
                    if environment == None or environment_found.lower() != environment.lower():
                        if environment_found not in skipped_environments:
                            skipped_environments.append(environment_found)
                        continue

                path = repo_path + "/" + name
                objects[path] = (type)
            else:
                print("type {0} NOT SUPPORTED YET!".format(type))
                sys.exit()

    if len(skipped_environments) > 0:
        print("Skipping files for environment(s): {0}".format(', '.join(sorted(skipped_environments))))

    return objects


#	Print a script that is not going to be executed.
def print_script(title,script):
    print(title)
    for statement in script:
        print(statement)
    print("")


def process_files(repo_path, cache, files, database_name, environment, force_flag, verbose_flag, dev_flag, dry_run_flag=False):

    objects = collect_objects(repo_path,files,database_name,environment)

    #   Directory name = postgres database name
    current_db = db(database=database_name)

    #	create tmp schema if not exist
    #   Skip tmp schema for bootstrap databases (postgres, template1) — we only
    #   connect there to run CREATE ROLE / CREATE DATABASE, not for schema work.
    if database_name not in ('postgres','template1'):
        current_db.create_tmp_schema()
    print("Deploying on {0}@{1} (environment: {2}):".format(current_db.db_name(),current_db.host(),environment if environment != None else 'none'))

    change_script = []
    change_script.append("--	START OF CHANGESCRIPT on " + database_name)
    change_script.append("BEGIN;")

    ps,cs,pr,po=process_objects(current_db, cache, objects, force_flag, dev_flag, verbose_flag)

    if dry_run_flag:
        #   Show everything that would run, in the order it would run, and stop.
        if len(ps) > 0:
            print_script("Dry run -- pre-script (autocommit):",ps)
        if len(cs) > 0:
            print_script("Dry run -- change script:",change_script + cs + [ "COMMIT;" ])
        for f in pr:
            print_script("Dry run -- privileges from {0}:".format(f.filename()),sqlpreprocessor.preprocess(f.contents()))
        if len(po) > 0:
            print_script("Dry run -- post-deployment:",[ "BEGIN;" ] + po + [ "COMMIT;" ])
        if len(ps) + len(cs) + len(pr) + len(po) == 0:
            print("Dry run -- nothing to do.")
        current_db.close_db()
        return 0

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

    execute_privileges(current_db,pr)

    execute_post_deployment(current_db,po,verbose_flag)

    #   Commit the cache only once everything has been deployed, so that a
    #   failing privilege or post-deployment script is retried on the next run.
    cache.commit()

    current_db.close_db()

    return len(change_script)


#	Compare the repository with the database and report. With fix_flag the
#	differences are resolved in one transaction (or, with dry_run_flag, the
#	script that would do so is printed). Returns the number of differences
#	left unresolved.
def check_files(repo_path, cache, files, database_name, environment, fix_flag, dry_run_flag, verbose_flag, dev_flag):

    objects = collect_objects(repo_path,files,database_name,environment)

    current_db = db(database=database_name)

    if database_name not in ('postgres','template1'):
        current_db.create_tmp_schema()
    print("Checking {0}@{1} (environment: {2}):".format(current_db.db_name(),current_db.host(),environment if environment != None else 'none'))

    (num_objects,num_differences,fix_script) = check_objects(current_db,cache,objects,dev_flag,verbose_flag)

    print("{0} object(s) checked, {1} difference(s).".format(num_objects,num_differences))

    if num_differences > 0 and fix_flag and len(fix_script) > 0:
        script = []
        script.append("--\tSTART OF FIX on " + database_name)
        script.append("BEGIN;")
        script.extend(fix_script)
        script.append("COMMIT;")
        script.append("--\tEND OF FIX")

        file_name = store_change_script(database_name,script)
        print("Fix script available at: " + file_name)

        if dry_run_flag:
            print_script("Dry run -- fix script:",script)
        else:
            result = current_db.execute(script,verbose_flag)
            if result != None:
                errorExit(result)

            #   The database now reflects these files
            for path in objects:
                if objects[path] in ['table','view','function','procedure','index']:
                    cache.add_entry(sqlfile(path))
            cache.commit()

            print("Fixed.")
            num_differences = 0

    current_db.close_db()

    return num_differences


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
    parser.add_argument('--env',help="deployment environment, selects data/<env>/ (overrides $DB_DEPLOYER_ENV)")
    parser.add_argument('--db',help="comma separated list of databases")
    parser.add_argument('--dev',action="store_true",help="only deploys changed files")
    parser.add_argument('--rebuild_cache',action="store_true",help="rebuild cache")
    parser.add_argument('--run',action="store_true",help="run actual deployment")
    parser.add_argument('--check',action="store_true",help="compare the database with the repo and report the differences")
    parser.add_argument('--fix',action="store_true",help="resolve the differences found by --check (implies --check)")
    parser.add_argument('--dry-run',dest='dry_run',action="store_true",help="with --run or --fix: print what would be executed, execute nothing")
    parser.add_argument('--verbose',action="store_true",help="show output")

    args = parser.parse_args()

    #  interpret arguments
    rebuild_cache_flag = args.rebuild_cache
    verbose_flag = args.verbose
    dev_flag = args.dev
    run_flag = args.run
    fix_flag = args.fix
    check_flag = args.check or args.fix
    dry_run_flag = args.dry_run

    if run_flag and check_flag:
        print("--run and --check/--fix are mutually exclusive.")
        sys.exit(-1)

    if dry_run_flag and not run_flag and not fix_flag:
        print("--dry-run needs --run or --fix.")
        sys.exit(-1)

    repo_path = args.repo if args.repo is not None else os.environ.get(constants._ENV_REPO)
    environment = args.env if args.env is not None else os.environ.get(ENV_ENVIRONMENT)
    if repo_path is None:
        print(f"Repo path not set. Use --repo or export {constants._ENV_REPO}.")
        sys.exit(-1)

    if environment != None:
        print("Environment: {0}".format(environment))
    else:
        print("")
        print("****************************************************************")
        print("*  WARNING: no environment set.")
        print("*  Environment-specific files (data/<env>/) are NOT deployed.")
        print("*  Use --env or export {0}.".format(ENV_ENVIRONMENT))
        print("****************************************************************")
        print("")

    i_did_something = False
    num_differences = 0
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
            process_files(repo_path,my_cache,all_files,current_db,environment,0,verbose_flag,dev_flag,dry_run_flag)
            i_did_something = True

        if check_flag == True:
            num_differences = num_differences + check_files(repo_path,my_cache,all_files,current_db,environment,fix_flag,dry_run_flag,verbose_flag,dev_flag)
            i_did_something = True

    if i_did_something == False:
        print("Dry-run complete. Add --run to deploy.\n")
        sys.exit(-1)

    if num_differences > 0:
        sys.exit(1)

if __name__ == "__main__":
    sys.exit(main())
