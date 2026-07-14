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
from sqlalchemy import create_engine
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


    def all_index_definitions(self, table_name):
        sql = """
            WITH indexDef AS
            (
                SELECT
                    idx.indrelid::REGCLASS::VARCHAR             AS table_name,
                    i.relname                                   AS index_name,
                    idx.indisunique::INT::VARCHAR               AS is_unique,
                    am.amname                                   AS index_type,
                    ARRAY
                    (
                        SELECT 
                            pg_get_indexdef(idx.indexrelid, k + 1, TRUE)
                        FROM
                            generate_subscripts(idx.indkey, 1)  AS k
                        ORDER BY 
                            k
                    )::VARCHAR                                  AS index_keys,
                    ((idx.indexprs IS NOT NULL) OR (idx.indkey::int[] @> array[0]))::INT::VARCHAR 
                                                                AS is_functional,
                    (idx.indpred IS NOT NULL)::INT::VARCHAR     AS is_partial
                FROM 
                    pg_index AS idx
                        JOIN pg_class i ON 
                            i.oid = idx.indexrelid
                        JOIN pg_am am ON 
                            i.relam = am.oid
                        JOIN pg_namespace NS ON 
                            i.relnamespace = NS.OID
                        JOIN pg_user U ON 
                            i.relowner = U.usesysid
                WHERE 
                    idx.indrelid :: REGCLASS :: VARCHAR ILIKE '{0}'
            )
            SELECT
                index_name,
                index_type || ':' || index_keys || ':' ||is_functional || ':' || is_partial 
            FROM
                indexDef
            WHERE
                table_name='{0}'
        """.format(table_name)

        table_name='datacheck_daily_discrepancy_result'
        sql = """
            SELECT DISTINCT
                --n.nspname as schema_name,
                --t.relname as table_name,
                i.relname as index_name,
                c.contype as index_type,
                a.attname as column_name
            FROM
                pg_class t
                    INNER JOIN pg_index ix ON
                        t.oid = ix.indrelid
                    INNER JOIN pg_constraint c ON
                        ix.indrelid = c.conrelid
                    INNER JOIN pg_class i ON
                        i.oid = ix.indexrelid
                    INNER JOIN pg_attribute a ON
                        a.attrelid = t.oid AND
                        a.attnum= ANY(string_to_array(textin(int2vectorout(ix.indkey)),' ')::int[])
                    INNER JOIN pg_namespace n ON
                        n.oid = t.relnamespace
            WHERE
                t.relname='{0}'
            ORDER BY
                 1,2,3
        """.format(table_name)

        with self._db_connection.cursor() as cursor:
            cursor.execute(sql)

            index_defs = {}
            index_cols = {}
            index_def = cursor.fetchone()
            while (index_def != None):
                index_name=index_def[0]
                index_type=index_def[1]
                column_name=index_def[2]

                if index_name not in index_defs.keys():
                    column = []
                    column.append(column_name)
                    index_cols[index_name]=column 
                    index_defs[index_name]=index_type
                else:
                    index_cols[index_name].append(column_name)

                index_def = cursor.fetchone()

        defs = {}
        for index_name in index_defs.keys():
            index_type=index_defs[index_name]
            index_columns=index_cols[index_name]

            defs[index_name]=index_type+':'+','.join(index_columns)
        return defs


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
                    NULL AS column_default,
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
