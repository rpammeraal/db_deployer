import os
import sys

from datetime import datetime
from multiprocessing import Process, current_process
from pathlib import Path

class util:

    _argv0=None
    _current_process_name=None
    _current_filename=None
    _log_to_file_only=False


    @classmethod
    def format_log(cls,pid,msg):
        msg = "{0}@[{1}]: {2}: {3}".format(datetime.now().strftime('%Y-%m-%d %H:%M:%S'),pid,util._process_name(),msg)
        return msg


    @classmethod
    def print_log(cls,msg,end=None,to_file_only=False,debug=False):
        if debug==True:
            print(f"util:24:start")

        if debug==True:
            print(f"util:31:cls._current_process_name={cls._current_process_name}")
        if cls._current_process_name==None:
            cls._current_process_name=util._determine_process_name(debug=debug)
        if debug==True:
            print(f"util:35:cls._current_process_name={cls._current_process_name}")

        if cls._current_filename==None:
            cls._determine_file_name()

        pid=os.getpid()
        pr=util.format_log(pid,msg)
        if to_file_only==False and cls._log_to_file_only==False:
            print(pr,end=end)

        if debug==True:
            print(f"util:42:cls._current_filename={cls._current_filename}")

        with open(cls._current_filename,"a") as f:
            f.write(f"{pr}\n")

        return cls._current_filename


    @classmethod
    def castToInt(cls,v):
        try:
            return int(v)
        except:
            pass
        return 0

    
    @classmethod
    def mkdir(cls,path):
        #   mkdir -p
        Path(path).mkdir(parents=True, exist_ok=True)


    @classmethod
    def strip_slash(cls,path):
        while path[-1]=='/':
            path=path[0:len(path)-1]
        return path


    @classmethod
    def _determine_process_name(cls,debug=False):
        if debug==True:
            print(f"util:71:start")
        current_process_name=''
        if debug:
            print(f"util:74:current_process={current_process()}")
        if current_process()!=None:
            if debug:
                print(f"util:77:name={current_process()}")
            current_process_name=current_process().name
        if debug:
            print(f"util:80:current_process_name={current_process_name}")
        if current_process_name=='MainProcess':
            if util._argv0==None:
                if debug:
                    print(f"util:84:_argv0={util._argv0}")
                util._argv0=os.path.basename(sys.argv[0])
            current_process_name=util._argv0

        if debug:
            print(f"util:89:done:current_process_name={current_process_name}")
        return current_process_name


    @classmethod
    def _determine_file_name(cls):
        if cls._current_process_name=='':
            cls._current_filename=f"/tmp/server.log"
        else:
            cls._current_filename=f"/tmp/server.{cls._current_process_name}.log"


    @classmethod
    def _process_name(cls):
        return cls._current_process_name


    @classmethod
    def _set_process_name(cls,name):
        cls._current_process_name=name
        cls._determine_file_name()


    @classmethod
    def _set_log_to_file_only(cls):
        cls._log_to_file_only=True


    @classmethod
    def _classinit_(cls):
        #   When spawning a new Process on Linux, we'll need to reset class variables, in
        #   order to get the correct process_name. Not a problem on Mac.
        cls._argv0=None
        cls._current_process_name=None
        cls._current_filename=None
        cls._log_to_file_only=False
