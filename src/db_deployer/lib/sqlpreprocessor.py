
#    sqlpreprocessor.py
#
#    Expand primitives in files conntaining Postgres functions.
#
import os
import re
import sys
import sqlparse
from sqlparse import tokens

from .db import db
from . import constants


class sqlpreprocessor:
    #    Main function to be used. This function go through the contents and expand primitives found.
    #    Usage:
    #    processed_contents []
    #    processed_contents = sqlpreprocessor.preprocess(contents[])

    @staticmethod
    def preprocess(contents):

        counter = 1
        fh = re.compile(r"___([A-Z_][A-Z0-9_]*)___\s*\(([^)]*)\)")
        for line_number, line in enumerate(contents):
            match = fh.search(line)
            while match:
                function_name = match.group(1)
                function_parameters = match.group(2).replace('(','').replace(')','').split(',')

                if function_name == 'ENV':
                    #   Deferred — no wrapper comments, resolved at execute time
                    replacement = ' '.join(sqlpreprocessor._process_env(function_parameters))
                else:
                    expanded = []
                    expanded.append(f'\n/* --- START GENERATED CODE FOR {function_name} {counter} */\n')
                    if function_name == 'ENCRYPT_FIELD':
                        expanded.extend(sqlpreprocessor._process_encrypted_field(function_parameters))
                    else:
                        print(f"Primitive {function_name} not supported. Exiting.")
                        sys.exit(-1)
                    expanded.append(f'\n/* --- END GENERATED CODE FOR {function_name} {counter} */\n')
                    replacement = ' '.join(expanded)

                line = re.sub(r"___([A-Z_][A-Z0-9_]*)___\s*\(([^)]*)\)",replacement,line,1)
                contents[line_number] = line
                counter = counter + 1
                match = fh.search(line)

        return contents


    #    Retrieve any parameters passed to a primitive.
    @staticmethod
    def _process_parameters(function_parameters):
        parameters = []

        in_set_flag = 0
        subset = []
        for i in function_parameters:
            for j in i.split(','):
                if j[:1] == '[':
                    in_set_flag = 1
                    j = j[1:]

                if in_set_flag == 1:
                    subset.append(j)

                if j[len(j) - 1:] == ']':
                    in_set_flag = 0
                    j = ','.join(subset)
                    j = j[:len(j) - 1]
                    subset = []

                if in_set_flag == 0:
                    parameters.append(j)

        return parameters


    #   Handles environment variable
    @staticmethod
    def _process_env(function_parameters):
        var_name = function_parameters[0].strip()
        if var_name not in os.environ:
            print(f"Missing environment variable {var_name} for ENV primitive. Exiting.")
            sys.exit(-1)

        #   Deferred substitution: emit a marker that does NOT match the preprocess
        #   regex (which requires ___NAME___ triple-underscore form). db.execute()
        #   resolves this to the actual value just before sending to postgres.
        return [f"@@ENV:{var_name}@@"]


    #   Generates a encrypted field
    @staticmethod
    def _process_encrypted_field(function_parameters):
        field=function_parameters[0]
        configuration_section=function_parameters[1]
        configuration_field=function_parameters[2]

        #   Read config file
        config_file = constants._INIT_FILE_LOCATION
        config = ConfigParser()
        config.read(config_file)
        secret = config.get(configuration_section, configuration_field)

        expanded=[]

        expanded.append(f"pgp_sym_encrypt({field}::TEXT,'{secret}'::TEXT) AS {field}")

        return expanded
        
    @staticmethod
    def _process_encrypted_field(function_parameters):
        field = function_parameters[0]
        configuration_section = function_parameters[1]
        configuration_field = function_parameters[2]

        env_name = f"{configuration_section}_{configuration_field}".upper()
        secret = os.environ.get(env_name)
        if secret is None:
            print(f"Missing environment variable {env_name} for ENCRYPT_FIELD primitive. Exiting.")
            sys.exit(-1)

        return [f"pgp_sym_encrypt({field}::TEXT,'{secret}'::TEXT) AS {field}"]
