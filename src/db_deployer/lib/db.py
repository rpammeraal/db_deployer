import psycopg2
import os
import re
import sys
import traceback
import inspect
import subprocess
import tempfile
import pickle
import time

from . import constants
from .tablefield import tablefield
from .util import util

class db:
    #    Holds connection to a database.
    #   Connection parameters are stored in '~/etc/data-core.ini'
    #   target_db_name should NOT be used (except for crt_sandbox, db_deployer), please use
    #   profile instead.
    def __init__(self,database=None,host=None,port=None,user=None,password=None,autocommit=True):

        self._db_parameter = {}
        self._db_parameter_extra = {}
        self._autocommit = autocommit

        #   Fall back to standard libpq env vars (PGHOST, PGPORT, PGUSER, PGDATABASE)
        self._host = host if host is not None else os.environ.get('PGHOST')
        self._port = port if port is not None else os.environ.get('PGPORT','5432')
        self._user = user if user is not None else os.environ.get('PGUSER')
        self._db_name = database if database is not None else os.environ.get('PGDATABASE')
        self._password = password   #   None => libpq consults ~/.pgpass

        missing = [n for n,v in [('host',self._host),('user',self._user),('database',self._db_name)] if not v]
        if missing:
            raise RuntimeError(f"Missing connection parameter(s): {missing}. Set PGHOST/PGUSER/PGDATABASE or pass explicitly.")

        self._construct_db_parameter()

        #    Connect to the PostgreSQL database server
        try:
            self._db_connection = psycopg2.connect(**self._db_parameter)
            self._db_connection.autocommit = autocommit

        except (Exception, psycopg2.DatabaseError) as error:
            raise RuntimeError(f"Error connecting to database: {error}") from error

        return None


    @classmethod
    def create_db_connection(cls,database=None,host=None,port=None,user=None,password=None,autocommit=True):
        return db(database=database,host=host,port=port,user=user,password=password,autocommit=autocommit)


    #   Split 'schema.name' into its parts; a bare name lives in public.
    @staticmethod
    def split_name(name):
        name = name.replace('"','')
        if name.find('.') != -1:
            return tuple(name.split('.',1))
        return ('public',name)


    #   Return {index_name: definition} for every index on 'schema.table' that
    #   does not back a constraint (those are reported by
    #   all_constraint_definitions()). The definition is what pg_get_indexdef()
    #   returns, so it includes the (schema-qualified) table name.
    def all_index_definitions(self, table_name):
        (schema,table) = db.split_name(table_name)

        sql = """
            SELECT
                i.relname,
                pg_get_indexdef(ix.indexrelid)
            FROM
                pg_index ix
                    JOIN pg_class t ON
                        t.oid = ix.indrelid
                    JOIN pg_class i ON
                        i.oid = ix.indexrelid
                    JOIN pg_namespace n ON
                        n.oid = t.relnamespace
            WHERE
                n.nspname = '{0}' AND
                t.relname = '{1}' AND
                NOT EXISTS
                (
                    SELECT NULL FROM pg_constraint c WHERE c.conindid = ix.indexrelid AND c.conrelid = ix.indrelid
                )
            ORDER BY
                1
        """.format(db.escape_quotes(schema),db.escape_quotes(table))

        defs = {}
        with self._db_connection.cursor() as cursor:
            cursor.execute(sql)
            for (index_name,definition) in cursor.fetchall():
                defs[index_name] = definition

        return defs


    #   Return {constraint_name: (type,definition)} for every constraint on
    #   'schema.table'. Not-null constraints (PostgreSQL 18 lists those here
    #   too) are left out: nullability is compared per column.
    def all_constraint_definitions(self, table_name):
        (schema,table) = db.split_name(table_name)

        sql = """
            SELECT
                c.conname,
                c.contype,
                pg_get_constraintdef(c.oid,TRUE)
            FROM
                pg_constraint c
                    JOIN pg_class t ON
                        t.oid = c.conrelid
                    JOIN pg_namespace n ON
                        n.oid = t.relnamespace
            WHERE
                n.nspname = '{0}' AND
                t.relname = '{1}' AND
                c.contype <> 'n'
            ORDER BY
                1
        """.format(db.escape_quotes(schema),db.escape_quotes(table))

        defs = {}
        with self._db_connection.cursor() as cursor:
            cursor.execute(sql)
            for (name,type,definition) in cursor.fetchall():
                defs[name] = (type,definition)

        return defs


    #   Return the relkind ('r' table, 'v' view, 'm' materialized view, ...)
    #   of 'schema.name', or None when there is no such relation.
    def relation_kind(self,name):
        sql = "SELECT relkind FROM pg_class WHERE oid = to_regclass('{0}')".format(db.escape_quotes(name))

        with self._db_connection.cursor() as cursor:
            cursor.execute(sql)
            row = cursor.fetchone()

        return None if row == None else row[0]


    #   Return the query behind view 'schema.name' as pg_get_viewdef() prints
    #   it, or None when there is no such view.
    def view_definition(self,name):
        sql = "SELECT pg_get_viewdef(to_regclass('{0}'),TRUE)".format(db.escape_quotes(name))

        with self._db_connection.cursor() as cursor:
            cursor.execute(sql)
            row = cursor.fetchone()

        return None if row == None else row[0]


    #   Return the dependents (see dependents()) of relation 'schema.name'.
    def relation_dependents(self,name):
        sql = "SELECT to_regclass('{0}')::OID".format(db.escape_quotes(name))

        with self._db_connection.cursor() as cursor:
            cursor.execute(sql)
            row = cursor.fetchone()

        if row == None or row[0] == None:
            return []

        return self.dependents('pg_class',row[0])


    #   Return [(oid,prokind,identity_arguments)] for every routine (all
    #   overloads) named 'schema.name'. An argument list following the name
    #   is ignored.
    def routine_signatures(self,routine_name):
        (schema,name) = db.split_name(routine_name.split('(')[0].strip())

        sql = """
            SELECT
                p.oid,
                p.prokind,
                pg_get_function_identity_arguments(p.oid)
            FROM
                pg_proc p
                    JOIN pg_namespace n ON
                        n.oid = p.pronamespace
            WHERE
                n.nspname = '{0}' AND
                p.proname = '{1}'
            ORDER BY
                3
        """.format(db.escape_quotes(schema),db.escape_quotes(name))

        with self._db_connection.cursor() as cursor:
            cursor.execute(sql)
            rows = cursor.fetchall()

        return [ (row[0],row[1],row[2]) for row in rows ]


    #   Return the complete CREATE statement of the routine with oid 'oid'.
    def routine_definition(self,oid):
        sql = "SELECT pg_get_functiondef({0})".format(oid)

        with self._db_connection.cursor() as cursor:
            cursor.execute(sql)
            row = cursor.fetchone()

        return None if row == None else row[0]


    #   Return {policy_name: (command,permissive,roles,using,with_check)} for
    #   every row level security policy on 'schema.table'.
    def policy_definitions(self,table_name):
        sql = """
            SELECT
                p.polname,
                p.polcmd,
                p.polpermissive,
                ARRAY(SELECT r.rolname FROM pg_roles r WHERE r.oid = ANY(p.polroles) ORDER BY 1)::TEXT,
                pg_get_expr(p.polqual,p.polrelid,TRUE),
                pg_get_expr(p.polwithcheck,p.polrelid,TRUE)
            FROM
                pg_policy p
            WHERE
                p.polrelid = to_regclass('{0}')
            ORDER BY
                1
        """.format(db.escape_quotes(table_name))

        defs = {}
        with self._db_connection.cursor() as cursor:
            cursor.execute(sql)
            for row in cursor.fetchall():
                defs[row[0]] = (row[1],row[2],row[3],row[4],row[5])

        return defs


    #   Return {trigger_name: definition} for every user trigger on
    #   'schema.table', as pg_get_triggerdef() prints it.
    def trigger_definitions(self,table_name):
        sql = """
            SELECT
                t.tgname,
                pg_get_triggerdef(t.oid,TRUE)
            FROM
                pg_trigger t
            WHERE
                t.tgrelid = to_regclass('{0}') AND
                NOT t.tgisinternal
            ORDER BY
                1
        """.format(db.escape_quotes(table_name))

        defs = {}
        with self._db_connection.cursor() as cursor:
            cursor.execute(sql)
            for (name,definition) in cursor.fetchall():
                defs[name] = definition

        return defs


    #   Return every object that depends -- directly, or through other
    #   dependents -- on the catalog object (classid,objid), or on one of its
    #   columns when attnum is given. classid is a catalog name ('pg_class',
    #   'pg_proc'). The result is a list of (type,identity) tuples as reported
    #   by pg_identify_object(), ordered so that no object precedes an object
    #   that depends on it: dropping them in list order never needs CASCADE.
    #   Only normal ('n') dependencies count; objects that go away with their
    #   owner anyway (indexes, constraints, sequences) are not reported.
    def dependents(self,classid,objid,attnum=None,found=None):
        if found == None:
            found = []

        subid_clause = 'TRUE' if attnum == None else "d.refobjsubid = {0}".format(attnum)

        sql = """
            WITH dependent AS
            (
                SELECT DISTINCT
                    CASE WHEN d.classid = 'pg_rewrite'::REGCLASS THEN 'pg_class'::REGCLASS::OID ELSE d.classid END AS classid,
                    CASE WHEN d.classid = 'pg_rewrite'::REGCLASS THEN r.ev_class ELSE d.objid END                AS objid
                FROM
                    pg_depend d
                        LEFT JOIN pg_rewrite r ON
                            d.classid = 'pg_rewrite'::REGCLASS AND
                            r.oid = d.objid
                WHERE
                    d.refclassid = '{0}'::REGCLASS AND
                    d.refobjid = {1} AND
                    {2} AND
                    d.deptype = 'n'
            )
            SELECT
                i.type,
                i.identity,
                dependent.classid::REGCLASS::TEXT,
                dependent.objid
            FROM
                dependent,
                LATERAL pg_identify_object(dependent.classid,dependent.objid,0) i
            WHERE
                NOT (dependent.classid = '{0}'::REGCLASS AND dependent.objid = {1})
            ORDER BY
                1,2
        """.format(classid,objid,subid_clause)

        with self._db_connection.cursor() as cursor:
            cursor.execute(sql)
            rows = cursor.fetchall()

        for (type,identity,dep_classid,dep_objid) in rows:
            if (type,identity) in found:
                continue

            #   Whatever depends on this dependent has to go first
            self.dependents(dep_classid,dep_objid,None,found)

            if (type,identity) not in found:
                found.append((type,identity))

        return found


    #   Return the dependents (see dependents()) of column 'column_name' of
    #   table 'schema.table'. An unknown table or column has no dependents.
    def column_dependents(self,table_name,column_name):
        sql = """
            SELECT
                attrelid,
                attnum
            FROM
                pg_attribute
            WHERE
                attrelid = to_regclass('{0}') AND
                attname = '{1}' AND
                NOT attisdropped
        """.format(db.escape_quotes(table_name),db.escape_quotes(column_name))

        with self._db_connection.cursor() as cursor:
            cursor.execute(sql)
            column = cursor.fetchone()

        if column == None:
            return []

        return self.dependents('pg_class',column[0],column[1])


    #   Return the oids of every routine (all overloads) named 'schema.name'.
    #   An argument list following the name is ignored.
    def routine_oids(self,routine_name):
        return [ signature[0] for signature in self.routine_signatures(routine_name) ]


    #   Return the dependents (see dependents()) of the routine(s) named
    #   'schema.name' -- all overloads, as the deployer drops by name.
    def routine_dependents(self,routine_name):
        found = []

        for oid in self.routine_oids(routine_name):
            self.dependents('pg_proc',oid,None,found)

        return found


    def change_db(self,new_db_name):
        self._target_db_name=new_db_name
        self._construct_db_parameter()


    def close_db(self):
        if self._db_connection is not None:
            self._db_connection.close()
        self._db_connection = None


    def create_table(self,schema,name,primary_key,df,overwrite=False):
        #   (re)create table as <schema>.<name> based on dataframe column header
        #   All columns will be VARCHAR
        sql = []
        
        sql.append(f"CREATE SCHEMA IF NOT EXISTS {schema};")
        if overwrite:
            sql.append(f"DROP TABLE IF EXISTS {schema}.{name} CASCADE;")
        sql.append(f"CREATE TABLE {schema}.{name} ( {primary_key} BIGINT PRIMARY KEY," + ' VARCHAR NULL, '.join(df.columns) + ' VARCHAR NULL );')
        self.execute(sql,verbose=True)


    def create_tmp_schema(self):
        list = []
        list.append('CREATE SCHEMA IF NOT EXISTS tmp;')
        result = self.execute(list, 0)
        if (result != None):
            print("An error has occurred: '{0}'".format(result))
            sys.exit(-1)


    def connection(self):
        return self._db_connection


    def engine(self):
        #   SQLAlchemy is not a dependency of the deployer -- only loaded when asked for
        from sqlalchemy import create_engine
        return create_engine(self.url())


    #   execute a batch of SQL commands. 
    def execute(self,batch,verbose=True,ignore_errors=False):
        try:
            with self._db_connection.cursor() as cursor:
                i = 0
                for sql in batch:
                    sql = sql.strip()
                    if sql[:2] != '--' and sql != ';':  #    cursor does NOT like blank statements
                        if verbose == True:
                            util.print_log(f"{sql}")
                            i = i + 1
                        try:
                            cursor.execute(self._resolve_env(sql))


                        except (Exception, psycopg2.DatabaseError) as error:
                            util.print_log(error)
                            if ignore_errors == False:
                                return error

                self._db_connection.commit()

        except (Exception, psycopg2.DatabaseError) as error:
            if error != None:
                return error

        return None


    def create_cursor(self, sql):
        try:
            conn = self.connection()
            cursor = conn.cursor()
            cursor.execute(sql)

        except psycopg2.DatabaseError:
            exc_type, exc_value, exc_traceback = sys.exc_info()
            stack = inspect.stack()[1]
            header = "\n=========================== SQL ERROR ============================\n"
            db_msg = f"db name={self.db_name()}:host={self.host()}\n"
            calling_class = stack[0].f_locals['self'].__class__.__name__
            calling_method = stack[3]
            origin = f"Origin Calling Method: {calling_class}.{calling_method}\n"
            body = '\n'.join(traceback.format_exception(exc_type, exc_value, exc_traceback))
            msg = "```" + header + db_msg + origin + body + "```"

            #   # Slack alert
            #   error_alert = alert()
            #   error_alert.send(slack_webhook_constant='webhook_url_data_check',msg=msg)

            # rollback connection and close cursor to abort
            conn.rollback()
            cursor.close()
        return cursor


    def credentials(self):
        return (self._host,self._port,self._user,self._password,self._db_name)


    def crt_table_statement(self,tabledef,schema_name,table_name,include_if_not_exists=None):
        output = []

        if include_if_not_exists!=None:
            include_if_not_exists='IF NOT EXISTS'
        else:
            include_if_not_exists=''

        output.append(f'CREATE TABLE {include_if_not_exists} "{schema_name}"."{table_name}"')
        output.append(f'(')

        new_table = []
        for f in tabledef:
            pk=''
            if tabledef[f].is_primary_key():
                pk=' PRIMARY KEY '

            new_table.append(f"\t{tabledef[f].name()}\t{tabledef[f].type()} {pk}")
        
        output.append(','.join(new_table))
        output.append(');')

        return ' '.join(output)
        

    def db_name(self):
        sql='SELECT current_database()';

        with self._db_connection.cursor() as cursor:
            cursor.execute(sql)

            record = cursor.fetchone()
            db_name = None
            while (record != None):
                db_name=record[0]
                record = cursor.fetchone()

        self._db_name=db_name
        return self._db_name


    def get_latest_timestamp(self,schema,table,exp='MAX(created_on)',verbose_flag=False):

        sql=f'SELECT {exp} FROM "{schema}"."{table}"; -- {self.db_name()}@{self.host()}'
        if verbose_flag:
            util.print_log(f"exp={exp}")
            util.print_log(f"{sql}")

        cursor = self.create_cursor(sql)
        r = None
        if not cursor.closed:
            r = cursor.fetchone()

        ts = None
        while (r != None):
            ts = r[0]
            r = cursor.fetchone()

        if ts==None:
            ts='1/1/1970'
        else:
            ts=f"'{ts}'"

        if verbose_flag:
            util.print_log(f"ts={ts}")

        return ts


    def host(self):
        return self._host


    def object_definition(self, tableName, verbose_flag=None):
        sql =  """
         SELECT 
            column_name,
            type_name,
            is_nullable,
            column_default,
            is_primary_key
        FROM
            (
                SELECT 
                    a.attname AS column_name,
                    pg_catalog.format_type(a.atttypid, a.atttypmod) AS type_name,
                    a.attnotnull::INT AS is_nullable,
                    pg_get_expr(ad.adbin,ad.adrelid) AS column_default,
                    a.attnum::INT AS ordinal_position,
                    i.indisprimary  AS is_primary_key
                FROM pg_attribute a
                    JOIN pg_class t ON
                        a.attrelid = t.oid
                    JOIN pg_namespace s ON
                        t.relnamespace = s.oid
                    LEFT JOIN pg_index i ON
                        a.attrelid = i.indrelid AND
                        a.attnum = ANY(i.indkey)
                    LEFT JOIN pg_attrdef ad ON
                        ad.adrelid = a.attrelid AND
                        ad.adnum = a.attnum
                WHERE 
                    s.nspname || '.' || t.relname = '{0}' AND 
                    a.attnum > 0 AND 
                    NOT a.attisdropped
            ) a
         ORDER BY 
            ordinal_position 
        """.format(tableName)
        with self._db_connection.cursor() as cursor:
            cursor.execute(sql)

            if verbose_flag!=None and verbose_flag!=False:
                print(sql)

            field_def = cursor.fetchone()
            table_def = {}
            while (field_def != None):
                table_def[field_def[0]] = tablefield(field_def[0], field_def[1],
                                                     (0
                                                      if field_def[2] == 1 else 1),
                                                     field_def[3],
                                                     field_def[4])
                field_def = cursor.fetchone()

        return table_def


    def object_exists(self, object_name, object_type, verbose_flag=None):

        object_exists_SQL = {
            'table':
            "SELECT DISTINCT 1 FROM pg_tables WHERE schemaname || '.' || tablename='{0}'".
            format(object_name),
            'schema':
            "SELECT DISTINCT 1 FROM pg_namespace WHERE nspname ='{0}'".format(
                object_name)
        }
        sql = object_exists_SQL.get(object_type, 'SELECT 0')

        if verbose_flag!=None and verbose_flag!=False:
            print(sql)
        with self._db_connection.cursor() as cursor:
            cursor.execute(sql)
            exists = cursor.fetchone()

            if (exists == None):
                exists = 0
            else:
                exists = exists[0]

        return exists


    def password(self):
        return self._db_parameter['password']


    def port(self):
        return self._db_parameter['port']


    def retrieve(self,sql,verbose_flag=None):
        try:
            cursor = self._db_connection.cursor()
        except:
            util.print_log(f"Error obtaining cursor to execute:{sql}")
            return []   #   CWIP: return empty resultset-- validate if [] is an empty resultset
                        #         or throw offsite exception class instance.
            

        if verbose_flag!=None and verbose_flag==True:
            util.print_log(sql)
        try:
            cursor.execute(sql)
        except:
            util.print_log(f"Error executing:{sql}")
            sys.exit(-1)

        return cursor.fetchall()


    def database_exists(self,db_name,verbose_flag=None):
        sql = f"SELECT 1 FROM pg_database WHERE datname = '{db_name}'"

        if verbose_flag!=None and verbose_flag!=False:
            print(f"{sql} -- {db_name}")

        with self._db_connection.cursor() as cursor:
            cursor.execute(sql,(db_name,))
            exists = cursor.fetchone()
        return 0 if exists is None else exists[0]


    def role_exists(self, role_name, verbose_flag=None):
        sql = f"SELECT TRUE FROM pg_roles WHERE rolname='{role_name}'"

        if verbose_flag!=None and verbose_flag!=False:
            print(f"{sql} -- {role_name}")

        with self._db_connection.cursor() as cursor:
            cursor.execute(sql,(role_name,))
            exists = cursor.fetchone()
        return 0 if exists is None else exists[0]


    #    Return a list of all schemas
    def schema_list(self):
        sql = 'SELECT schema_name FROM information_schema.schemata'

        with self._db_connection.cursor() as cursor:
            cursor.execute(sql)

            schema_list = []
            record = cursor.fetchone()
            while (record != None):
                schema_list.append(record[0])
                record = cursor.fetchone()

        return schema_list


    def set_permissions(self,verbose_flag=None):
        config = ConfigParser()
        config.read(constants._INIT_FILE_LOCATION)

        sql = """
            SELECT 
                owner,
                schema_name,
                table_name,
                permission,
                group_name
            FROM 
                etl.schema_table_permission
            ORDER BY
                owner,
                schema_name,
                table_name,
                permission,
                group_name
            ;
            """

        if verbose_flag!=None and verbose_flag!=False:
            print(sql)

        with self._db_connection.cursor() as cursor:
            cursor.execute(sql)
            r = cursor.fetchone()
            schema_processed = []
            all_sql = []
            while (r != None):
                owner=r[0]
                schema_name=r[1]
                table_name=r[2]
                permission=r[3]
                group_name=r[4]

                owner_password=None
                if owner==None or owner=='':
                    owner=self.user()
                    owner_password = self.password()
                else:
                    owner_password=None
                    if owner==self.user():
                        owner_password=self.password()
                    else:
                        if config.has_option(constants._CONFIG_DATABASE_ACCOUNT_SECTION,owner):
                            owner_password = config.get(constants._CONFIG_DATABASE_ACCOUNT_SECTION,owner)
                        else:
                            print(f"User {owner} not set up. Continuing")
                            continue

                p_db=db(profile='n/a',user=owner,password=owner_password,host=self.host(),port=self.port(),target_db_name=self.db_name())


                subject=schema_name
                if table_name!=None:
                    subject=f"{subject}.{table_name}"

                tables_spec=''
                if table_name==None:
                    tables_spec=f'ALL TABLES IN SCHEMA {schema_name}'
                else:
                    tables_spec=f"{schema_name}.{table_name}"

                all_sql.append(f'GRANT {permission} ON {tables_spec} TO GROUP {group_name};')

                if schema_name not in schema_processed:
                    sql=f'GRANT USAGE ON SCHEMA {schema_name} TO GROUP {group_name};'
                    all_sql.append(sql)
                    schema_processed.append(sql)

                util.print_log(f"Granting permissions as user {p_db.user()}:")
                p_db.execute(all_sql,True)
                all_sql=[]

                #    Process next
                r = cursor.fetchone()


    def connection_data(self):
        #   The output of this function is meant to be displayed
        #   by the caller to confirm the database the caller is operating on
        return f"{self.user()}:{self.db_name()}@{self.host()}"


    def start_psql_session(self,db=None,cmd=None,file=None,silent=None,flags=None,outputfile=None):
        #   Note! This replaces the current process.

        extra_arg=[]
        if cmd!=None:
            extra_arg.append("-t")
            extra_arg.append("-q")
            extra_arg.append("-c")
            extra_arg.append(f'{cmd}')

        if file!=None:
            extra_arg.append("-f")
            extra_arg.append(file)

        if db==None:
            db=self._db_name
            
        if silent!=None:
            extra_arg.append("-q ")

        if flags!=None:
            for x in flags.split():
                extra_arg.append(x)

        redirect=''
        if outputfile!=None:
            extra_arg.append('> ')
            extra_arg.append({outputfile})
            

        args=["psql",f"postgresql://{self._user}:{self._password}@{self._host}:{self._port}/{db}","-P","pager=off","-v","ON_ERROR_STOP=1"]

        if len(extra_arg)>0:
            args.extend(extra_arg)
        #if redirect!=None and redirect!='':
            #args.append(redirect)
        #print(f'args={' '.join(args)}')
        return os.execvp("psql",args)


    def start_psql_session_as_pipe(self,db=None,cmd=None,file=None,silent=None):
        arg=''
        if cmd!=None:
            arg=arg + "-t -q -c \"{0}\"".format(cmd)

        if file!=None:
            arg=arg + "-f {0}".format(file)

        if db==None:
            db=self._db_name
            
        if silent!=None:
            arg=arg + "-q "

        return os.popen("psql postgresql://{0}:{1}@{2}:{3}/{4} -P pager=off -v ON_ERROR_STOP=1 {5}".format(self._user,self._password,self._host,self._port,db,arg))

        return os.system(cmd)


    def table_exists(self,schemaname,tablename,verbose_flag=None):
        tl=self.table_list(schemaname)
        if verbose_flag:
            util.print_log(f"{tl}")
        if f"{schemaname}.{tablename}" in tl:
            return True
        return False


    def table_list(self,schemaname,verbose_flag=None):
        sql="SELECT schemaname || '.' || relname FROM pg_catalog.pg_statio_user_tables WHERE schemaname = '{0}' ORDER BY pg_relation_size(relid) DESC".format(schemaname)
        if verbose_flag:
            util.print_log(f"{sql}")

        with self._db_connection.cursor() as cursor:
            cursor.execute(sql)

            table_list = []
            record = cursor.fetchone()
            while (record != None):
                table_list.append(record[0])
                record = cursor.fetchone()

        return table_list


    def user(self):
        return self._db_parameter['user']


    def url(self):
        if self._db_connection==None:
            return "postgresql://{0}:{1}@{2}:{3}".format(self.user(),self.password(),self.host(),self.port())
        else:
            return "postgresql://{0}:{1}@{2}:{3}/{4}".format(self.user(),self.password(),self.host(),self.port(),self.db_name())


    def vacuum_full_all(self,table=None,op='FULL,ANALYZE'):

        util.print_log(f"Running vacuum on {self.db_name()}:")
        where_clause = " WHERE schemaname NOT IN ('information_schema', 'tmp' ) AND schemaname NOT ILIKE 'pg_%' AND schemaname NOT ILIKE 'dbt%' "
        if table != None:
            where_clause = where_clause + "AND  schemaname || '.' || tablename = '{0}'".format(table)

        sql = "SELECT schemaname || '.' || tablename FROM pg_catalog.pg_tables {0} ORDER BY 1".format(where_clause)

        with self._db_connection.cursor() as cursor:
            cursor.execute(sql)

            v = cursor.fetchone()
            while (v != None):
                table = v[0]

                for o in op.split(','):
                    to_exec = f"VACUUM {o} {table};"
                    util.print_log(to_exec)
                    exec_cursor = self._db_connection.cursor()
                    exec_cursor.execute(to_exec)

                v = cursor.fetchone()


    def _construct_db_parameter(self):
        self._db_parameter['host'] = self._host
        self._db_parameter['port'] = self._port
        self._db_parameter['user'] = self._user
        self._db_parameter['database'] = self._db_name
        if self._password is not None:
            self._db_parameter['password'] = self._password


    def _resolve_env(self,sql):
        def _sub(m):
            name = m.group(1)
            value = os.environ.get(name)
            if value is None:
                raise RuntimeError(f"Environment variable {name} referenced by @@ENV:...@@ marker is not set at execute time.")
            return value
        return re.sub(r'@@ENV:([A-Za-z_][A-Za-z0-9_]*)@@',_sub,sql)


    @staticmethod
    def escape_quotes(str):
        return str.replace("'","''")
