#	A tablefield object contains a table field definition:
#	-	name: name of field
#	-	type: type of field
#	-	nullable_flag: specifies of the field can have NULLs
#	-	default: optional default


class tablefield:

    def __init__(self, name, field_type, nullable_flag, default, is_primary_key=None):
        self._name = name
        self._type = field_type
        self._nullable_flag = nullable_flag if nullable_flag != None else 0
        self._default = default
        if is_primary_key==None:
            is_primary_key=False
        self._is_primary_key = is_primary_key

        if self._type[0:17]=='character varying':
            self._type='varchar'


    def compare(self, t):
        a=self.__repr__()
        b=t.__repr__()
        return a==b


    def name(self):
        return self._name


    def setType(self,n):
        self._type=n


    def type(self):
        return self._type


    def nullable_flag(self):
        return self._nullable_flag


    def default(self):
        return self._default


    def is_primary_key(self):
        return self._is_primary_key


    def __repr__(self):
        return f'name={self._name}:type={self._type}:is_primary_key={self._is_primary_key}'

