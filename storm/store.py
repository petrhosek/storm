#
# Copyright (c) 2006 Canonical
#
# Written by Gustavo Niemeyer <gustavo@niemeyer.net>
#
# This file is part of Storm Object Relational Mapper.
#
# <license text goes here>
#
from weakref import WeakValueDictionary, WeakKeyDictionary

from storm.info import get_cls_info, get_obj_info, get_info
from storm.variables import Variable
from storm.expr import (
    Expr, Select, Insert, Update, Delete, Column, JoinExpr, Count, Max, Min,
    Avg, Sum, Eq, And, Asc, Desc, compile_python, compare_columns)
from storm.exceptions import (
    WrongStoreError, NotFlushedError, OrderLoopError, UnorderedError,
    NotOneError, FeatureError, CompileError)
from storm import Undef


__all__ = ["Store"]

PENDING_ADD = 1
PENDING_REMOVE = 2


class Store(object):

    _result_set_factory = None

    def __init__(self, database):
        self._connection = database.connect()
        self._cache = WeakValueDictionary()
        self._ghosts = WeakKeyDictionary()
        self._dirty = set()
        self._order = {} # (info, info) = count

    @staticmethod
    def of(obj):
        try:
            return get_obj_info(obj).get("store")
        except AttributeError:
            return None

    def execute(self, statement, params=None, noresult=False):
        self.flush()
        return self._connection.execute(statement, params, noresult)

    def close(self):
        self._connection.close()

    def commit(self):
        self.flush()
        self._connection.commit()
        for obj_info in self._iter_ghosts():
            del obj_info["store"]
        for obj_info in self._iter_cached():
            obj_info.save()
        self._ghosts.clear()

    def rollback(self):
        infos = set()
        for obj_info in self._iter_dirty():
            infos.add(obj_info)
        for obj_info in self._iter_ghosts():
            infos.add(obj_info)
        for obj_info in self._iter_cached():
            infos.add(obj_info)

        for obj_info in infos:
            self._remove_from_cache(obj_info)

            obj_info.restore()

            if obj_info.get("store") is self:
                self._add_to_cache(obj_info)
                self._enable_change_notification(obj_info)

        self._ghosts.clear()
        self._dirty.clear()
        self._connection.rollback()

    def get(self, cls, key):
        self.flush()

        if type(key) != tuple:
            key = (key,)

        cls_info = get_cls_info(cls)

        assert len(key) == len(cls_info.primary_key)

        primary_vars = []
        for column, variable in zip(cls_info.primary_key, key):
            if not isinstance(variable, Variable):
                variable = column.variable_factory(value=variable)
            primary_vars.append(variable)

        obj_info = self._cache.get((cls, tuple(primary_vars)))
        if obj_info is not None:
            return obj_info.obj

        where = compare_columns(cls_info.primary_key, primary_vars)

        select = Select(cls_info.columns, where,
                        default_tables=cls_info.table, limit=1)

        result = self._connection.execute(select)
        values = result.get_one()
        if values is None:
            return None
        return self._load_object(cls_info, result, values)

    def find(self, cls_spec, *args, **kwargs):
        self.flush()
        if type(cls_spec) is tuple:
            cls_spec_info = tuple(get_cls_info(cls) for cls in cls_spec)
            where = get_where_for_args(args, kwargs)
        else:
            cls_spec_info = get_cls_info(cls_spec)
            where = get_where_for_args(args, kwargs, cls_spec)
        return self._result_set_factory(self, cls_spec_info, where)

    def using(self, *tables):
        def process(obj):
            if not (isinstance(obj, basestring) or isinstance(obj, Expr)):
                obj = get_cls_info(obj).table
            elif isinstance(obj, JoinExpr):
                if obj.left is not Undef:
                    left = process(obj.left)
                else:
                    left = Undef
                right = process(obj.right)
                obj = obj.__class__(left, right, obj.on)
            return obj
        return self._table_set(self, [process(x) for x in tables])

    def new(self, cls, *args, **kwargs):
        obj = cls(*args, **kwargs)
        self.add(obj)
        return obj

    def add(self, obj):
        obj_info = get_obj_info(obj)

        store = obj_info.get("store")
        if store is not None and store is not self:
            raise WrongStoreError("%s is part of another store" % repr(obj))

        pending = obj_info.get("pending")

        if pending is PENDING_ADD:
            pass
        elif pending is PENDING_REMOVE:
            del obj_info["pending"]
            # obj_info.event.emit("added")
        else:
            if store is None:
                obj_info.save()
                obj_info["store"] = self
            elif not self._is_ghost(obj_info):
                return # It's fine already.
            else:
                self._set_alive(obj_info)

            obj_info["pending"] = PENDING_ADD
            self._set_dirty(obj_info)
            obj_info.event.emit("added")

    def remove(self, obj):
        obj_info = get_obj_info(obj)

        if obj_info.get("store") is not self:
            raise WrongStoreError("%s is not in this store" % repr(obj))

        pending = obj_info.get("pending")

        if pending is PENDING_REMOVE:
            pass
        elif pending is PENDING_ADD:
            del obj_info["pending"]
            self._set_ghost(obj_info)
            self._set_clean(obj_info)
        elif not self._is_ghost(obj_info):
            obj_info["pending"] = PENDING_REMOVE
            self._set_dirty(obj_info)

    def reload(self, obj):
        obj_info = get_obj_info(obj)
        cls_info = obj_info.cls_info
        if obj_info.get("store") is not self or self._is_ghost(obj_info):
            raise WrongStoreError("%s is not in this store" % repr(obj))
        if "primary_vars" not in obj_info:
            raise NotFlushedError("Can't reload an object if it was "
                                  "never flushed")
        where = compare_columns(cls_info.primary_key, obj_info["primary_vars"])
        select = Select(cls_info.columns, where,
                        default_tables=cls_info.table, limit=1)
        result = self._connection.execute(select)
        values = result.get_one()
        self._set_values(obj_info, cls_info.columns, result, values)
        obj_info.checkpoint()
        self._set_clean(obj_info)

    def add_flush_order(self, before, after):
        pair = (get_obj_info(before), get_obj_info(after))
        try:
            self._order[pair] += 1
        except KeyError:
            self._order[pair] = 1

    def remove_flush_order(self, before, after):
        pair = (get_obj_info(before), get_obj_info(after))
        try:
            self._order[pair] -= 1
        except KeyError:
            pass

    def flush(self):
        predecessors = {}
        for (before_info, after_info), n in self._order.iteritems():
            if n > 0:
                before_set = predecessors.get(after_info)
                if before_set is None:
                    predecessors[after_info] = set((before_info,))
                else:
                    before_set.add(before_info)

        while self._dirty:
            for obj_info in self._dirty:
                for before_info in predecessors.get(obj_info, ()):
                    if before_info in self._dirty:
                        break # A predecessor is still dirty.
                else:
                    break # Found an item without dirty predecessors.
            else:
                raise OrderLoopError("Can't flush due to ordering loop")
            self._dirty.discard(obj_info)
            self._flush_one(obj_info)

        self._order.clear()

    def _flush_one(self, obj_info):
        cls_info = obj_info.cls_info

        pending = obj_info.pop("pending", None)

        if pending is PENDING_REMOVE:
            expr = Delete(compare_columns(cls_info.primary_key,
                                          obj_info["primary_vars"]),
                          cls_info.table)
            self._connection.execute(expr, noresult=True)

            self._disable_change_notification(obj_info)
            self._set_ghost(obj_info)
            self._remove_from_cache(obj_info)

        elif pending is PENDING_ADD:
            columns = []
            variables = []

            for column in cls_info.columns:
                variable = obj_info.variables[column]
                if variable.is_defined():
                    columns.append(column)
                    variables.append(variable)

            expr = Insert(columns, variables, cls_info.table)

            result = self._connection.execute(expr)

            self._fill_missing_values(obj_info, result)

            self._enable_change_notification(obj_info)
            self._set_alive(obj_info)
            self._add_to_cache(obj_info)

            obj_info.checkpoint()

        else:

            changes = {}
            for column in cls_info.columns:
                variable = obj_info.variables[column]
                if variable.has_changed() and variable.is_defined():
                    changes[column] = variable

            if changes:
                expr = Update(changes,
                              compare_columns(cls_info.primary_key,
                                              obj_info["primary_vars"]),
                              cls_info.table)
                self._connection.execute(expr, noresult=True)

                self._add_to_cache(obj_info)

            obj_info.checkpoint()

        obj_info.event.emit("flushed")

    def _fill_missing_values(self, obj_info, result):
        cls_info = obj_info.cls_info

        missing_columns = []
        for column in cls_info.columns:
            if not obj_info.variables[column].is_defined():
                missing_columns.append(column)

        if missing_columns:
            primary_key = cls_info.primary_key
            primary_vars = obj_info.primary_vars

            for variable in primary_vars:
                if not variable.is_defined():
                    where = result.get_insert_identity(primary_key,
                                                       primary_vars)
                    break
            else:
                where = compare_columns(primary_key, primary_vars)

            result = self._connection.execute(Select(missing_columns, where))

            self._set_values(obj_info, missing_columns,
                             result, result.get_one())

    def _load_objects(self, cls_spec_info, result, values):
        if type(cls_spec_info) is not tuple:
            return self._load_object(cls_spec_info, result, values)
        else:
            objects = []
            values_start = values_end = 0
            for cls_info in cls_spec_info:
                values_end += len(cls_info.columns)
                obj = self._load_object(cls_info, result,
                                        values[values_start:values_end])
                objects.append(obj)
                values_start = values_end
            return tuple(objects)

    def _load_object(self, cls_info, result, values, obj=None):
        if obj is None:
            primary_vars = []
            columns = cls_info.columns
            is_null = True
            for i in cls_info.primary_key_pos:
                value = values[i]
                if value is not None:
                    is_null = False
                variable = columns[i].variable_factory(value=value,
                                                       from_db=True)
                primary_vars.append(variable)
            if is_null:
                return None
            obj_info = self._cache.get((cls_info.cls, tuple(primary_vars)))
            if obj_info is not None:
                return obj_info.obj
            obj = cls_info.cls.__new__(cls_info.cls)

        obj_info = get_obj_info(obj)
        obj_info["store"] = self

        self._set_values(obj_info, cls_info.columns, result, values)

        obj_info.save()

        self._add_to_cache(obj_info)
        self._enable_change_notification(obj_info)

        load = getattr(obj, "__load__", None)
        if load is not None:
            load()

        obj_info.save_attributes()

        return obj

    def _set_values(self, obj_info, columns, result, values):
        for column, value in zip(columns, values):
            if value is None:
                obj_info.variables[column].set(value, from_db=True)
            else:
                result.set_variable(obj_info.variables[column], value)


    def _is_dirty(self, obj_info):
        return obj_info in self._dirty

    def _set_dirty(self, obj_info):
        self._dirty.add(obj_info)

    def _set_clean(self, obj_info):
        self._dirty.discard(obj_info)

    def _iter_dirty(self):
        return self._dirty


    def _is_ghost(self, obj_info):
        return obj_info in self._ghosts

    def _set_ghost(self, obj_info):
        self._ghosts[obj_info] = True

    def _set_alive(self, obj_info):
        self._ghosts.pop(obj_info, None)

    def _iter_ghosts(self):
        return self._ghosts.keys()


    def _add_to_cache(self, obj_info):
        # Notice that the obj_info may be added back to the cache even
        # though it already has primary_vars, since the object could be
        # removed from cache, rolled back, and reinserted (commit() will
        # save() obj_info with primary_vars).
        cls_info = obj_info.cls_info
        old_primary_vars = obj_info.get("primary_vars")
        if old_primary_vars is not None:
            self._cache.pop((cls_info.cls, old_primary_vars), None)
        new_primary_vars = tuple(variable.copy()
                                 for variable in obj_info.primary_vars)
        self._cache[cls_info.cls, new_primary_vars] = obj_info
        obj_info["primary_vars"] = new_primary_vars

    def _remove_from_cache(self, obj_info):
        primary_vars = obj_info.get("primary_vars")
        if primary_vars is not None:
            del self._cache[obj_info.cls_info.cls, primary_vars]
            del obj_info["primary_vars"]

    def _iter_cached(self):
        return self._cache.values()


    def _enable_change_notification(self, obj_info):
        obj_info.event.hook("changed", self._variable_changed)

    def _disable_change_notification(self, obj_info):
        obj_info.event.unhook("changed", self._variable_changed)

    def _variable_changed(self, obj_info, variable, old_value, new_value):
        if new_value is not Undef:
            self._dirty.add(obj_info)


class ResultSet(object):

    def __init__(self, store, cls_spec_info, where, tables=Undef):
        self._store = store
        self._cls_spec_info = cls_spec_info
        self._where = where
        self._tables = tables
        self._order_by = Undef
        self._offset = Undef
        self._limit = Undef
        self._distinct = False

    def copy(self):
        result_set = object.__new__(self.__class__)
        result_set.__dict__.update(self.__dict__)
        return result_set

    def config(self, distinct=None, offset=None, limit=None):
        if distinct is not None:
            self._distinct = distinct
        if offset is not None:
            self._offset = offset
        if limit is not None:
            self._limit = limit
        return self

    def _get_select(self, **kwargs):
        if type(self._cls_spec_info) is tuple:
            columns = []
            default_tables = []
            for cls_info in self._cls_spec_info:
                columns.append(cls_info.columns)
                default_tables.append(cls_info.table)
        else:
            columns = self._cls_spec_info.columns
            default_tables = self._cls_spec_info.table
        return Select(columns, self._where, self._tables, default_tables,
                      self._order_by, offset=self._offset, limit=self._limit,
                      distinct=self._distinct)

    def __iter__(self):
        result = self._store._connection.execute(self._get_select())
        for values in result:
            yield self._store._load_objects(self._cls_spec_info,
                                            result, values)

    def __getitem__(self, index):
        if isinstance(index, (int, long)):
            if index == 0:
                result_set = self
            else:
                if self._offset is not Undef:
                    index += self._offset
                result_set = self.copy()
                result_set.config(offset=index, limit=1)
            obj = result_set.any()
            if obj is None:
                raise IndexError("Index out of range")
            return obj

        if not isinstance(index, slice):
            raise IndexError("Can't index ResultSets with %r" % (index,))
        if index.step is not None:
            raise IndexError("Stepped slices not yet supported: %r"
                             % (index.step,))

        offset = self._offset
        limit = self._limit

        if index.start is not None:
            if offset is Undef:
                offset = index.start
            else:
                offset += index.start
            if limit is not Undef:
                limit = max(0, limit - index.start)

        if index.stop is not None:
            if index.start is None:
                new_limit = index.stop
            else:
                new_limit = index.stop - index.start
            if limit is Undef or limit > new_limit:
                limit = new_limit

        return self.copy().config(offset=offset, limit=limit)

    def any(self):
        """Return a single item from the result set.

        See also one(), first(), and last().
        """
        select = self._get_select()
        select.limit = 1
        result = self._store._connection.execute(select)
        values = result.get_one()
        if values:
            return self._store._load_objects(self._cls_spec_info,
                                             result, values)
        return None

    def first(self):
        """Return the first item from an ordered result set.

        Will raise UnorderedError if the result set isn't ordered.

        See also last(), one(), and any().
        """
        if self._order_by is Undef:
            raise UnorderedError("Can't use first() on unordered result set")
        return self.any()

    def last(self):
        """Return the last item from an ordered result set.

        Will raise UnorderedError if the result set isn't ordered.

        See also first(), one(), and any().
        """
        if self._order_by is Undef:
            raise UnorderedError("Can't use last() on unordered result set")
        if self._limit is not Undef:
            raise FeatureError("Can't use last() with a slice "
                               "of defined stop index")
        select = self._get_select()
        select.offset = Undef
        select.limit = 1
        select.order_by = []
        for expr in self._order_by:
            if isinstance(expr, Desc):
                select.order_by.append(expr.expr)
            elif isinstance(expr, Asc):
                select.order_by.append(Desc(expr.expr))
            else:
                select.order_by.append(Desc(expr))
        result = self._store._connection.execute(select)
        values = result.get_one()
        if values:
            return self._store._load_objects(self._cls_spec_info,
                                             result, values)
        return None

    def one(self):
        """Return one item from a result set containing at most one item.

        Will raise NotOneError if the result set contains more than one item.

        See also first(), one(), and any().
        """
        select = self._get_select()
        # limit could be 1 due to slicing, for instance.
        if select.limit is not Undef and select.limit > 2:
            select.limit = 2
        result = self._store._connection.execute(select)
        values = result.get_one()
        if result.get_one():
            raise NotOneError("one() used with more than one result available")
        if values:
            return self._store._load_objects(self._cls_spec_info,
                                             result, values)
        return None

    def order_by(self, *args):
        if self._offset is not Undef or self._limit is not Undef:
            raise FeatureError("Can't reorder a sliced result set")
        self._order_by = args
        return self

    def remove(self):
        if self._offset is not Undef or self._limit is not Undef:
            raise FeatureError("Can't remove a sliced result set")
        if type(self._cls_spec_info) is tuple:
            raise FeatureError("Removing not yet supported with tuple finds")
        self._store._connection.execute(Delete(self._where,
                                               self._cls_spec_info.table),
                                        noresult=True)

    def _aggregate(self, expr, column=None):
        if type(self._cls_spec_info) is tuple:
            default_tables = [cls_info.table
                              for cls_info in self._cls_spec_info]
        else:
            default_tables = self._cls_spec_info.table
        select = Select(expr, self._where, self._tables, default_tables)
        result = self._store._connection.execute(select)
        value = result.get_one()[0]
        if column is None:
            return value
        else:
            variable = column.variable_factory()
            result.set_variable(variable, value)
            return variable.get()

    def count(self):
        return int(self._aggregate(Count()))

    def max(self, column):
        return self._aggregate(Max(column), column)

    def min(self, column):
        return self._aggregate(Min(column), column)

    def avg(self, column):
        return float(self._aggregate(Avg(column)))

    def sum(self, column):
        return self._aggregate(Sum(column), column)

    def values(self, *columns):
        if not columns:
            raise FeatureError("values() takes at least one column "
                               "as argument")
        select = self._get_select()
        select.columns = columns
        result = self._store._connection.execute(select)
        if len(columns) == 1:
            variable = columns[0].variable_factory()
            for values in result:
                result.set_variable(variable, values[0])
                yield variable.get()
        else:
            variables = [column.variable_factory() for column in columns]
            for values in result:
                for variable, value in zip(variables, values):
                    result.set_variable(variable, value)
                yield tuple(variable.get() for variable in variables)

    def set(self, *args, **kwargs):
        if type(self._cls_spec_info) is tuple:
            raise FeatureError("Setting isn't supportted with tuple finds")

        if not (args or kwargs):
            return

        changes = {}
        cls = self._cls_spec_info.cls

        for expr in args:
            if (not isinstance(expr, Eq) or
                not isinstance(expr.expr1, Column) or
                not isinstance(expr.expr2, (Column, Variable))):
                raise FeatureError("Unsupported set expression: %r" %
                                   repr(expr))
            changes[expr.expr1] = expr.expr2

        for key, value in kwargs.items():
            column = getattr(cls, key)
            if value is None:
                changes[column] = None
            elif isinstance(value, Expr):
                if not isinstance(value, Column):
                    raise FeatureError("Unsupported set expression: %r" %
                                       repr(value))
                changes[column] = value
            else:
                changes[column] = column.variable_factory(value=value)

        expr = Update(changes, self._where, self._cls_spec_info.table)
        self._store._connection.execute(expr, noresult=True)

        try:
            cached = self.cached()
        except CompileError:
            for obj_info in self._store._iter_cached():
                if isinstance(obj_info.obj, cls):
                    self._store.reload(obj_info.obj)
        else:
            changes = changes.items()
            for obj in cached:
                for column, value in changes:
                    variables = get_obj_info(obj).variables
                    if value is None:
                        pass
                    elif isinstance(value, Variable):
                        value = value.get()
                    else:
                        value = variables[value].get()
                    variables[column].set(value)
                    variables[column].checkpoint()

    def cached(self):
        if type(self._cls_spec_info) is tuple:
            raise FeatureError("Cached finds not supported with tuples")
        if self._tables is not Undef:
            raise FeatureError("Cached finds not supported with custom tables")
        if self._where is Undef:
            match = None
        else:
            match = compile_python(self._where)
            name_to_column = dict((column.name, column)
                                  for column in self._cls_spec_info.columns)
            def get_column(name, name_to_column=name_to_column):
                return obj_info.variables[name_to_column[name]].get()
        objects = []
        cls = self._cls_spec_info.cls
        for obj_info in self._store._iter_cached():
            if (obj_info.cls_info is self._cls_spec_info and
                (match is None or match(get_column))):
                objects.append(obj_info.obj)
        return objects


class TableSet(object):
    
    def __init__(self, store, tables):
        self._store = store
        self._tables = tables

    def find(self, cls_spec, *args, **kwargs):
        self._store.flush()
        if type(cls_spec) is tuple:
            cls_spec_info = tuple(get_cls_info(cls) for cls in cls_spec)
            where = get_where_for_args(args, kwargs)
        else:
            cls_spec_info = get_cls_info(cls_spec)
            where = get_where_for_args(args, kwargs, cls_spec)
        return self._store._result_set_factory(self._store, cls_spec_info,
                                               where, self._tables)


Store._result_set_factory = ResultSet
Store._table_set = TableSet


def get_where_for_args(args, kwargs, cls=None):
    equals = list(args)
    if kwargs:
        if cls is None:
            raise FeatureError("Can't determine class that keyword "
                               "arguments are associated with")
        for key, value in kwargs.items():
            column = getattr(cls, key)
            equals.append(Eq(column, column.variable_factory(value=value)))
    if equals:
        return And(*equals)
    return Undef
