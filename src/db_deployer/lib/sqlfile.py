import hashlib
import os
import sys
import sqlparse
import re
from sqlparse import tokens

from . import constants


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

    
    #	Return the object type (e.g. table, schema, ...) in lower case.
    def object_type(self):

        if len(self._sql) == 0:
            return None

        header = self._sql[0].lower()
        word_array = []
        word_array = header.split()

        if len(word_array)>1 and word_array[1] and word_array[1] == 'materialized':
            header = header.replace('materialized ', '')
            self._sub_type = 'materialized'

        if len(word_array)>1 and word_array[1] and word_array[1] == 'unique' and word_array[2] and word_array[2]=='index':
            header = header.replace('unique ', 'index ')
            self._sub_type = 'unique'

        if len(word_array)>1 and word_array[1] and word_array[1] == 'or' and word_array[2] == 'replace':
            #   do a case insensitive replace on the actual header line,
            #   as the 'OR REPLACE' will screw up (later generated) code.
            pattern = re.compile("or replace ", re.IGNORECASE)
            self._sql[0] = pattern.sub("", self._sql[0])
            header = self._sql[0].lower()

		
        object_type = None

        if len(header.split())>1:
            object_type = header.split()[1]
        else:
            object_type = ''

        if object_type == 'view':
            #   inspect schema name to determine cube or view
            (schema, object_name) = (header.split()[2]).split(".",2)
            if schema == 'datacube':
                object_type = schema
        elif object_type == 'foreign':
            object_type = 'foreign_table'

        if object_type not in ['database','schema', 'table', 'function', 'view', 'datacube', 'index', 'foreign_table', 'role']:
            object_type = 'data'

            #   determine if privilege based on directory path:
        path_array=self._path.split('/')
        if path_array[-2]=='privilege':
            object_type='privilege'
        elif path_array[-2] == 'role':
            object_type = 'role'
        elif path_array[-2] == 'database':
            object_type = 'database'

        return object_type


    def object_sub_type(self):
        return self._sub_type


    #	Returns the name of the object 'as-is' -- case is not modified
    def object_name(self):
        object_name = ''
        if (self.object_type() == 'schema' or self.object_type() == 'table'):
            object_name = self._sql[0].split()[2]
            object_name = object_name[:-1] if object_name.endswith(
                ';') else object_name  #	remove trailing ';'

        elif (self.object_type() == 'function'):
            #	include parameters for function, exclude RETURNS clause
            object_name = ' '.join(self._sql[0].split()[2:])
            object_name = object_name[:object_name.find('RETURNS')]

        elif self.object_type() in ['view', 'datacube']:
            #   account for optional keyword materialized
            if self._sql[0].split()[1].lower() == 'materialized':
                object_name = self._sql[0].split()[3]
            else:
                object_name = self._sql[0].split()[2]

        elif (self.object_type() in ['data','privilege']):
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

