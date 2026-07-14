import os
import sys
import datetime
from os.path import basename

from . import constants

class cache:
    def __init__(self,repo_root):
        self._global_root = repo_root
        self._shelve_path = "{0}/{1}".format(self._global_root,constants._CACHE_FILENAME)
        self._dictionary = {}
        self._read_entries()
        self._file_changed = []


    #	Returns all index definitions for the specified table.
    def add_entry(self, file):
        value = "{0}:{1}".format(file.hash(), file.timestamp())
        key = self._get_local_path(file.path())
        self._dictionary[key] = value;
        return


    def commit(self):
        with open(self._shelve_path, 'w') as file:
            for key in sorted(self._dictionary.keys()):
                file.write("{0}:{1}\n".format(key, self._dictionary[key]))
        self._file_changed = []


    def has_file_changed(self, file):
        if file.path() in self._file_changed or basename(file.path()) in self._file_changed:
            return 1

        key = self._get_local_path(file.path())
        if key in self._dictionary:
            value = self._dictionary[key]
            if(value != None):
                contents = []
                contents = value.split(':')
                hash = contents[0]
                timestamp = contents[1]
                if (str(hash) == str(file.hash())) and (str(timestamp) == str(file.timestamp())):
                    return 0

        return 1    #   not found or changed


    def set_file_changed(self, path):
        if path not in self._file_changed:
             self._file_changed.append(path)


    def dump(self):
        print("dump:")
        for key in sorted(self._dictionary.keys()):
            print("file {0}:contents {1}".format(key, self._dictionary[key]))


    def _read_entries(self):
        self._dictionary = {}
        try:
            with open(self._shelve_path,'r') as file:
                for line_num,line in enumerate(file,1):
                    line = line.strip('\n')
                    if not line:
                        continue
                    contents = line.split(':',3)
                    if len(contents) < 3:
                        print(f"Warning: malformed cache line {line_num} skipped: {line!r}")
                        continue
                    self._dictionary[contents[0]] = "{0}:{1}".format(contents[1],contents[2])
        except (IOError,OSError):
            return
        return


    def _get_local_path(self, path):
         return path.replace(self._global_root, '')

