import hashlib
import os
import sys
import sqlparse
import re
from sqlparse import tokens

from . import constants


#	Matches a leading 'OR REPLACE' clause: CREATE OR REPLACE <object type> ...
#	Anchored, so that an 'or replace' occurring further down in the statement
#	(in a view body or a function body) is left alone.
OR_REPLACE_PATTERN = re.compile(r'^(\s*CREATE)\s+OR\s+REPLACE\s+',re.IGNORECASE)


#	Matches an index creation statement:
#	CREATE [UNIQUE] INDEX [CONCURRENTLY] [IF NOT EXISTS] <name> ON [ONLY] <table> ...
#	The index name is optional -- PostgreSQL generates one when it is omitted.
INDEX_HEADER_PATTERN = re.compile(
    r'^CREATE\s+(?:UNIQUE\s+)?INDEX\s+'
    r'(?:CONCURRENTLY\s+)?'
    r'(?:IF\s+NOT\s+EXISTS\s+)?'
    r'(?:(?P<index>(?!ON\s)[^\s(]+)\s+)?'
    r'ON\s+(?:ONLY\s+)?(?P<table>[^\s(]+)',
    re.IGNORECASE)


#	Matches a policy creation statement:
#	CREATE POLICY <name> ON <table> ...
#	Both identifiers may be double-quoted (and a quoted table name may be
#	schema-qualified), so a token is any run of quoted or unquoted characters.
POLICY_HEADER_PATTERN = re.compile(
    r'^CREATE\s+POLICY\s+'
    r'(?P<policy>(?:"[^"]*"|[^\s"])+)\s+'
    r'ON\s+(?P<table>(?:"[^"]*"|[^\s"(;])+)',
    re.IGNORECASE)


#	Matches a trigger creation statement:
#	CREATE [OR REPLACE] [CONSTRAINT] TRIGGER <name> { BEFORE | AFTER | INSTEAD OF } <events> ON <table> ...
#	The first ON following the name introduces the table -- no event keyword
#	contains ON as a word of its own.
TRIGGER_HEADER_PATTERN = re.compile(
    r'^CREATE\s+(?:OR\s+REPLACE\s+)?(?:CONSTRAINT\s+)?TRIGGER\s+'
    r'(?P<trigger>(?:"[^"]*"|[^\s"])+)\s+'
    r'.*?\bON\s+(?P<table>(?:"[^"]*"|[^\s"(;])+)',
    re.IGNORECASE | re.DOTALL)


#	sqlfile contains  a definition of an SQL Object
#	(such as a table, function, schema, etc.)
class sqlfile:

    #	Usage my_sql_file = SQLFile("<path to file>.sql")
    def __init__(self, path):
        self._path = path
        self._sql = []
        self._hash = None
        self._timestamp = os.path.getmtime(path)
        self._sub_type = None

        c = []
        try:
            f = open(path)

            #	Ignore lines starting with comment
            for line in f:
                line = line.strip()
                if (line[:2] != '--'):
                    comment_start = line.find('--')
                    if (comment_start != -1):
                        line = line[:comment_start]
                    c.append(line)

            #	Split contents up in valid SQL statements
            new_sql = []
            for statement in sqlparse.parse(' '.join(c)):
                tokens = [
                    stm for stm in statement.tokens
                    if not isinstance(stm, sqlparse.sql.Comment)
                ]

                new_statement = sqlparse.sql.TokenList(tokens)

                new_sql.append(new_statement)

            #	Create array with SQL statements
            for i in new_sql:
                sql = str(i).strip()
                if (len(sql) > 0):
                    self._sql.append(sql)

        finally:
            f.close()

        contents = ' '.join(self._sql)
        #   self._hash.update(contents.encode('utf_8'))
        self._hash = hashlib.md5(contents.encode('utf_8')).hexdigest()


    #	Return the contents in an array.
    def contents(self):
        return self._sql


    #	Dumps the contents to stdout, prepended with line numbers.
    def dump(self, title):
        print("start dump:" + title)
        i = 1
        for line in self._sql:
            print("{0}: {1}".format(str(i).zfill(4), ' '.join(line.split())))
            i = i + 1
        print("end dump:" + title)

    #	For the next objects, we assume that the database object is defined on the 1st line (comments are ignored)


    def filename(self):
        return os.path.basename(self._path)


    def timestamp(self):
        return self._timestamp


    def hash(self):
        return self._hash


    #	Return the position of the first word following an optional 'IF NOT EXISTS'
    #	clause, starting to look at word position 'start'. When no such clause is
    #	present, 'start' is returned unchanged.
    @staticmethod
    def skip_if_not_exists(words,start):
        if len(words) >= start + 3 and [w.upper() for w in words[start:start + 3]] == ['IF','NOT','EXISTS']:
            return start + 3

        return start


    #	Return the position of the object type word (TABLE, VIEW, FUNCTION, ...),
    #	skipping an optional 'OR REPLACE' clause.
    @staticmethod
    def skip_or_replace(words):
        if len(words) > 2 and words[1].upper() == 'OR' and words[2].upper() == 'REPLACE':
            return 3

        return 1


    #	Return the signature ('<name>(<arguments>)') of a function or procedure,
    #	with the name starting at word position 'start'. The argument list is
    #	delimited by its matching closing parenthesis, so any RETURNS clause and
    #	anything following it are excluded.
    @staticmethod
    def routine_signature(words,start):
        signature = ' '.join(words[start:])

        depth = 0
        for position,character in enumerate(signature):
            if character == '(':
                depth = depth + 1
            elif character == ')':
                depth = depth - 1
                if depth == 0:
                    return signature[:position + 1]

        #	No (complete) argument list found -- fall back to the bare name
        name = words[start] if len(words) > start else ''
        return name[:-1] if name.endswith(';') else name


    #	Parse an index creation statement and return an (index_name,table_name) tuple.
    #	Returns None when the statement does not create an index. The optional
    #	UNIQUE, CONCURRENTLY and IF NOT EXISTS clauses are skipped, and index_name
    #	is returned as '' for an unnamed index.
    @staticmethod
    def parse_index_header(statement):
        match = INDEX_HEADER_PATTERN.match(statement.strip())
        if match == None:
            return None

        index_name = match.group('index')

        return (index_name if index_name != None else '',match.group('table'))


    #	Return an (index_name,table_name) tuple for every index created in this file.
    def index_definitions(self):
        definitions = []

        for statement in self._sql:
            index_header = sqlfile.parse_index_header(statement)
            if index_header != None:
                definitions.append(index_header)

        return definitions


    #	Parse a policy creation statement and return a (policy_name,table_name) tuple.
    #	Returns None when the statement does not create a policy.
    @staticmethod
    def parse_policy_header(statement):
        match = POLICY_HEADER_PATTERN.match(statement.strip())
        if match == None:
            return None

        return (match.group('policy'),match.group('table'))


    #	Return a (policy_name,table_name) tuple for every policy created in this file.
    def policy_definitions(self):
        definitions = []

        for statement in self._sql:
            policy_header = sqlfile.parse_policy_header(statement)
            if policy_header != None:
                definitions.append(policy_header)

        return definitions


    #	Parse a trigger creation statement and return a (trigger_name,table_name) tuple.
    #	Returns None when the statement does not create a trigger.
    @staticmethod
    def parse_trigger_header(statement):
        match = TRIGGER_HEADER_PATTERN.match(statement.strip())
        if match == None:
            return None

        return (match.group('trigger'),match.group('table'))


    #	Return a (trigger_name,table_name) tuple for every trigger created in this file.
    def trigger_definitions(self):
        definitions = []

        for statement in self._sql:
            trigger_header = sqlfile.parse_trigger_header(statement)
            if trigger_header != None:
                definitions.append(trigger_header)

        return definitions

    
    #	Return the object type (e.g. table, schema, ...) in lower case.
    def object_type(self):

        if len(self._sql) == 0:
            return None

        header = self._sql[0].lower()
        word_array = []
        word_array = header.split()

        or_replace = False

        if len(word_array)>2 and word_array[1] == 'or' and word_array[2] == 'replace':
            #   Drop the 'OR REPLACE' clause from the header, so that everything
            #   following it can be examined as if it were a plain CREATE. The
            #   statement itself is only modified further down -- and only for
            #   objects that this deployer drops before recreating them.
            or_replace = True
            word_array = word_array[:1] + word_array[3:]
            header = ' '.join(word_array)

        if len(word_array)>1 and word_array[1] and word_array[1] == 'materialized':
            header = header.replace('materialized ','',1)
            self._sub_type = 'materialized'

        if len(word_array)>2 and word_array[1] and word_array[1] == 'unique' and word_array[2] and word_array[2]=='index':
            header = header.replace('unique ','index ',1)
            self._sub_type = 'unique'

        object_type = None

        if len(header.split())>1:
            object_type = header.split()[1]
        else:
            object_type = ''

        if object_type == 'view':
            #   inspect schema name to determine cube or view
            #   (skip an optional IF NOT EXISTS clause first)
            words = header.split()
            name_position = sqlfile.skip_if_not_exists(words,2)
            qualified_name = words[name_position] if len(words) > name_position else ''

            if qualified_name.find(".") != -1:
                schema = qualified_name.split(".",1)[0]
                if schema == 'datacube':
                    object_type = schema
        elif object_type == 'foreign':
            object_type = 'foreign_table'

        if object_type not in ['database','schema', 'table', 'function', 'procedure', 'view', 'datacube', 'index', 'foreign_table', 'role']:
            object_type = 'data'

            #   determine if privilege based on directory path:
        path_array=self._path.split('/')
        if path_array[-2]=='privilege':
            object_type='privilege'
        elif path_array[-2] == 'role':
            object_type = 'role'
        elif path_array[-2] == 'database':
            object_type = 'database'
        elif path_array[-2] in ['data','post_deployment']:
            #   Anything goes in these directories -- the contents are never
            #   parsed, the directory alone decides the type.
            object_type = path_array[-2]

        if or_replace == True and object_type in ['function','procedure','view','datacube']:
            #   These objects are dropped before they are recreated, and the
            #   'OR REPLACE' clause would screw up the (later generated) code,
            #   so remove it from the statement itself. Object types that are
            #   deployed as-is keep their 'OR REPLACE', as that is what makes
            #   them re-runnable.
            self._sql[0] = OR_REPLACE_PATTERN.sub(r'\1 ',self._sql[0],count=1)

        return object_type


    def object_sub_type(self):
        return self._sub_type


    #	Returns the name of the object 'as-is' -- case is not modified
    def object_name(self):
        object_name = ''

        if (self.object_type() == 'schema' or self.object_type() == 'table'):
            words = self._sql[0].split()
            #   Skip optional IF NOT EXISTS (3 words)
            name_position = sqlfile.skip_if_not_exists(words,2)
            object_name = words[name_position] if len(words) > name_position else ''
            object_name = object_name[:-1] if object_name.endswith(';') else object_name

        elif (self.object_type() == 'function' or self.object_type() == 'procedure'):
            #	include parameters for the routine, exclude the RETURNS clause
            words = self._sql[0].split()
            object_name = sqlfile.routine_signature(words,sqlfile.skip_or_replace(words) + 1)

        elif self.object_type() in ['view', 'datacube']:
            words = self._sql[0].split()
            #   CREATE [OR REPLACE] [MATERIALIZED] VIEW [IF NOT EXISTS] <name>
            type_position = sqlfile.skip_or_replace(words)
            name_position = type_position + 1
            if len(words) > type_position and words[type_position].lower() == 'materialized':
                name_position = type_position + 2
            name_position = sqlfile.skip_if_not_exists(words,name_position)
            object_name = words[name_position] if len(words) > name_position else ''

        elif self.object_type() == 'index':
            #   A file may create more than one index -- report the first one.
            definitions = self.index_definitions()
            if len(definitions) > 0:
                object_name = definitions[0][0]

        elif (self.object_type() in ['data','privilege','post_deployment']):
            object_name = self._path

        elif (self.object_type() == 'foreign_table'):
            object_name = os.path.basename(self._path)

        elif self.object_type() == 'role':
            #   Scan the SQL for CREATE ROLE / ALTER ROLE — works whether the statement
            #   is bare or wrapped in a DO block.
            joined = ' '.join(self._sql)
            m = re.search(r'\b(?:CREATE|ALTER)\s+ROLE\s+(\w+)',joined,re.IGNORECASE)
            if m:
                object_name = m.group(1)

        elif self.object_type() == 'database':
            joined = ' '.join(self._sql)
            m = re.search(r'\b(?:CREATE|ALTER)\s+DATABASE\s+(\w+)',joined,re.IGNORECASE)
            if m:
                object_name = m.group(1)

        return object_name


    def path(self):
        return self._path;


    #	Change the object name. A global search and replace is performed throughout the contents
    def set_object_name(self, newObjectName):
        #	Set newObjectName across the board
        currentObjectName = self.object_name()
        for index, line in enumerate(self._sql):
            self._sql[index] = line.replace(currentObjectName, newObjectName)
            #	print self._sql[index]

