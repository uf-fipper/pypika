from copy import copy
from functools import reduce
from itertools import chain
import operator
import builtins
from typing import (
    Any,
    Callable,
    Generic,
    Iterable,
    List,
    Optional,
    Sequence,
    Tuple as TypedTuple,
    Type,
    Union,
    Set,
    cast,
    TypeVar,
    overload,
    TYPE_CHECKING,
)
from typing_extensions import Self

from pypika.enums import Dialects, JoinType, ReferenceOption, SetOperation, Order
from pypika.terms import (
    ArithmeticExpression,
    Criterion,
    EmptyCriterion,
    Field,
    Function,
    Index,
    Rollup,
    Star,
    Term,
    Tuple,
    ValueWrapper,
    Criterion,
    PeriodCriterion,
    WrappedConstantValue,
    WrappedConstant,
)
from pypika.utils import (
    JoinException,
    QueryException,
    RollupException,
    SetOperationException,
    builder,
    format_alias_sql,
    format_quotes,
    ignore_copy,
    SQLPart,
)

__author__ = "Timothy Heys"
__email__ = "theys@kayak.com"


_T = TypeVar("_T")
SchemaT = TypeVar("SchemaT", bound="Schema")
if TYPE_CHECKING:
    from typing_extensions import TypeVar

    QueryBuilderType = TypeVar("QueryBuilderType", bound="QueryBuilder", covariant=True, default="QueryBuilder")
else:
    QueryBuilderType = TypeVar("QueryBuilderType", bound="QueryBuilder", covariant=True)


class Selectable(Term):
    def __init__(self, alias: Optional[str]) -> None:
        self.alias = alias

    @builder
    def as_(self, alias: str):
        self.alias = alias

    def field(self, name: str) -> Field:
        return Field(name, table=self)

    @property
    def star(self) -> Star:
        return Star(self)

    @ignore_copy
    def __getattr__(self, name: str) -> Field:
        return self.field(name)

    @ignore_copy
    def __getitem__(self, name: str) -> Field:
        return self.field(name)

    def get_table_name(self) -> str:
        if self.alias is None:
            raise TypeError("expect str, got None")
        return self.alias

    def get_sql(self, **kwargs) -> str:
        raise NotImplementedError


class AliasedQuery(Selectable, SQLPart):
    def __init__(self, name: str, query: Optional[Selectable] = None) -> None:
        super().__init__(alias=name)
        self.name = name
        self.query = query

    def get_sql(self, **kwargs: Any) -> str:
        if self.query is None:
            return self.name
        return self.query.get_sql(**kwargs)

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, AliasedQuery) and self.name == other.name

    def __hash__(self) -> int:
        return hash(str(self.name))


class Schema(SQLPart):
    def __init__(self, name: str, parent: Optional["Schema"] = None) -> None:
        self._name = name
        self._parent = parent

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, Schema) and self._name == other._name and self._parent == other._parent

    def __ne__(self, other: Any) -> bool:
        return not self.__eq__(other)

    @ignore_copy
    def __getattr__(self, item: str) -> "Table":
        return Table(item, schema=self)

    def get_sql(self, quote_char: Optional[str] = None, **kwargs: Any) -> str:
        # FIXME escape
        schema_sql = format_quotes(self._name, quote_char)

        if self._parent is not None:
            return "{parent}.{schema}".format(
                parent=self._parent.get_sql(quote_char=quote_char, **kwargs),
                schema=schema_sql,
            )

        return schema_sql


class Database(Schema):
    @ignore_copy
    def __getattr__(self, item: str) -> Schema:
        return Schema(item, parent=self)


class Table(Selectable, Generic[QueryBuilderType]):
    @staticmethod
    def _init_schema(schema: Union[str, list, tuple, Schema, None]) -> Optional[Schema]:
        # This is a bit complicated in order to support backwards compatibility. It should probably be cleaned up for
        # the next major release. Schema is accepted as a string, list/tuple, Schema instance, or None
        if isinstance(schema, Schema):
            return schema
        if isinstance(schema, (list, tuple)):
            return reduce(lambda obj, s: Schema(s, parent=obj), schema[1:], Schema(schema[0]))
        if schema is not None:
            return Schema(schema)
        return None

    def __init__(
        self,
        name: str,
        schema: Union[str, list, tuple, Schema, None] = None,
        alias: Optional[str] = None,
        query_cls: Optional[Type["Query[QueryBuilderType]"]] = None,
    ) -> None:
        super().__init__(alias)
        self._table_name = name
        self._schema = self._init_schema(schema)
        self._query_cls: Type["Query[QueryBuilderType]"] = query_cls or Query
        self._for: Optional[Criterion] = None
        self._for_portion: Optional[PeriodCriterion] = None
        if not issubclass(self._query_cls, Query):
            raise TypeError("Expected 'query_cls' to be subclass of Query")

    def get_table_name(self) -> str:
        return self.alias or self._table_name

    def get_sql(self, **kwargs: Any) -> str:
        quote_char = kwargs.get("quote_char")
        # FIXME escape
        table_sql = format_quotes(self._table_name, quote_char)

        if self._schema is not None:
            table_sql = "{schema}.{table}".format(schema=self._schema.get_sql(**kwargs), table=table_sql)

        if self._for:
            table_sql = "{table} FOR {criterion}".format(table=table_sql, criterion=self._for.get_sql(**kwargs))
        elif self._for_portion:
            table_sql = "{table} FOR PORTION OF {criterion}".format(
                table=table_sql, criterion=self._for_portion.get_sql(**kwargs)
            )

        return format_alias_sql(table_sql, self.alias, **kwargs)

    @builder
    def for_(self, temporal_criterion: Criterion):
        if self._for:
            raise AttributeError("'Query' object already has attribute for_")
        if self._for_portion:
            raise AttributeError("'Query' object already has attribute for_portion")
        self._for = temporal_criterion

    @builder
    def for_portion(self, period_criterion: PeriodCriterion):
        if self._for_portion:
            raise AttributeError("'Query' object already has attribute for_portion")
        if self._for:
            raise AttributeError("'Query' object already has attribute for_")
        self._for_portion = period_criterion

    def __str__(self) -> str:
        return self.get_sql(quote_char='"')

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, Table):
            return False

        if self._table_name != other._table_name:
            return False

        if self._schema != other._schema:
            return False

        if self.alias != other.alias:
            return False

        return True

    def __repr__(self) -> str:
        if self._schema:
            return "Table('{}', schema='{}')".format(self._table_name, self._schema)
        return "Table('{}')".format(self._table_name)

    def __ne__(self, other: Any) -> bool:
        return not self.__eq__(other)

    def __hash__(self) -> int:
        return hash(str(self))

    def select(self, *terms: Union[int, float, str, bool, Term, Field]) -> "QueryBuilderType":
        """
        Perform a SELECT operation on the current table

        :param terms:
            Type:  list[expression]

            A list of terms to select. These can be any type of int, float, str, bool or Term or a Field.

        :return:  QueryBuilder
        """
        return self._query_cls.from_(self).select(*terms)

    def update(self) -> "QueryBuilderType":
        """
        Perform an UPDATE operation on the current table

        :return: QueryBuilder
        """
        return self._query_cls.update(self)

    def insert(self, *terms: Union[int, float, str, bool, Term, Field]) -> "QueryBuilderType":
        """
        Perform an INSERT operation on the current table

        :param terms:
            Type: list[expression]

            A list of terms to select. These can be any type of int, float, str, bool or  any other valid data

        :return: QueryBuilder
        """
        return self._query_cls.into(self).insert(*terms)


def make_tables(
    *names: Union[TypedTuple[str, str], str], query_cls: "Optional[Type[Query[QueryBuilderType]]]" = None, **kwargs: Any
) -> List[Table[QueryBuilderType]]:
    """
    Shortcut to create many tables. If `names` param is a tuple, the first
    position will refer to the `_table_name` while the second will be its `alias`.
    Any other data structure will be treated as a whole as the `_table_name`.
    """
    tables: List["Table[QueryBuilderType]"] = []
    for name in names:
        if isinstance(name, tuple):
            if len(name) == 2:
                t = Table(
                    name=name[0],
                    alias=name[1],
                    schema=kwargs.get("schema"),
                    query_cls=query_cls,
                )
            else:
                raise TypeError("expect tuple[str, str] or str, got a tuple with {} element(s)".format(len(name)))
        else:
            t = Table(
                name=name,
                schema=kwargs.get("schema"),
                query_cls=kwargs.get("query_cls"),
            )
        tables.append(t)
    return tables


class Column(SQLPart):
    """Represents a column."""

    def __init__(
        self,
        column_name: str,
        column_type: Optional[str] = None,
        nullable: Optional[bool] = None,
        default: object = None,
    ) -> None:
        self.name = column_name
        self.type = column_type
        self.nullable = nullable
        self.default = default if default is None or isinstance(default, Term) else ValueWrapper(default)

    def get_name_sql(self, **kwargs: Any) -> str:
        quote_char = kwargs.get("quote_char")

        column_sql = "{name}".format(
            name=format_quotes(self.name, quote_char),
        )

        return column_sql

    def get_sql(self, **kwargs: Any) -> str:
        column_sql = "{name}{type}{nullable}{default}".format(
            name=self.get_name_sql(**kwargs),
            type=" {}".format(self.type) if self.type else "",
            nullable=" {}".format("NULL" if self.nullable else "NOT NULL") if self.nullable is not None else "",
            default=" {}".format("DEFAULT " + self.default.get_sql(**kwargs)) if self.default else "",
        )

        return column_sql

    def __str__(self) -> str:
        return self.get_sql(quote_char='"')


def make_columns(*names: Union[TypedTuple[str, str], str]) -> List[Column]:
    """
    Shortcut to create many columns. If `names` param is a tuple, the first
    position will refer to the `name` while the second will be its `type`.
    Any other data structure will be treated as a whole as the `name`.
    """
    columns = []
    for name in names:
        if isinstance(name, tuple):
            if len(name) == 2:
                column = Column(column_name=name[0], column_type=name[1])
            else:
                raise TypeError("expect tuple[str, str] or str, got a tuple with {} element(s)".format(len(name)))
        else:
            column = Column(column_name=name)
        columns.append(column)

    return columns


class PeriodFor(SQLPart):
    def __init__(self, name: str, start_column: Union[str, Column], end_column: Union[str, Column]) -> None:
        self.name = name
        self.start_column = start_column if isinstance(start_column, Column) else Column(start_column)
        self.end_column = end_column if isinstance(end_column, Column) else Column(end_column)

    def get_sql(self, **kwargs: Any) -> str:
        quote_char = kwargs.get("quote_char")

        period_for_sql = "PERIOD FOR {name} ({start_column_name},{end_column_name})".format(
            name=format_quotes(self.name, quote_char),
            start_column_name=self.start_column.get_name_sql(**kwargs),
            end_column_name=self.end_column.get_name_sql(**kwargs),
        )

        return period_for_sql


# for typing in Query's methods
_TableClass = Table


class Query(Generic[QueryBuilderType]):
    """
    Query is the primary class and entry point in pypika. It is used to build queries iteratively using the builder
    design
    pattern.

    This class is immutable.
    """

    @classmethod
    def _builder(cls, **kwargs: Any) -> "QueryBuilderType":
        return QueryBuilder(**kwargs)

    @classmethod
    def from_(cls, table: Union[Selectable, str], **kwargs: Any) -> "QueryBuilderType":
        """
        Query builder entry point.  Initializes query building and sets the table to select from.  When using this
        function, the query becomes a SELECT query.

        :param table:
            Type: Table or str

            An instance of a Table object or a string table name.

        :returns QueryBuilder
        """
        return cls._builder(**kwargs).from_(table)

    @classmethod
    def create_table(cls, table: Union[str, Table]) -> "CreateQueryBuilder":
        """
        Query builder entry point. Initializes query building and sets the table name to be created. When using this
        function, the query becomes a CREATE statement.

        :param table: An instance of a Table object or a string table name.

        :return: CreateQueryBuilder
        """
        return CreateQueryBuilder().create_table(table)

    @classmethod
    def drop_database(cls, database: Union[Database, str]) -> "DropQueryBuilder":
        """
        Query builder entry point. Initializes query building and sets the table name to be dropped. When using this
        function, the query becomes a DROP statement.

        :param database: An instance of a Database object or a string database name.

        :return: DropQueryBuilder
        """
        return DropQueryBuilder().drop_database(database)

    @classmethod
    def drop_table(cls, table: Union[str, Table]) -> "DropQueryBuilder":
        """
        Query builder entry point. Initializes query building and sets the table name to be dropped. When using this
        function, the query becomes a DROP statement.

        :param table: An instance of a Table object or a string table name.

        :return: DropQueryBuilder
        """
        return DropQueryBuilder().drop_table(table)

    @classmethod
    def drop_user(cls, user: str) -> "DropQueryBuilder":
        """
        Query builder entry point. Initializes query building and sets the table name to be dropped. When using this
        function, the query becomes a DROP statement.

        :param user: String user name.

        :return: DropQueryBuilder
        """
        return DropQueryBuilder().drop_user(user)

    @classmethod
    def drop_view(cls, view: str) -> "DropQueryBuilder":
        """
        Query builder entry point. Initializes query building and sets the table name to be dropped. When using this
        function, the query becomes a DROP statement.

        :param view: String view name.

        :return: DropQueryBuilder
        """
        return DropQueryBuilder().drop_view(view)

    @classmethod
    def into(cls, table: Union[Table, str], **kwargs: Any) -> "QueryBuilderType":
        """
        Query builder entry point.  Initializes query building and sets the table to insert into.  When using this
        function, the query becomes an INSERT query.

        :param table:
            Type: Table or str

            An instance of a Table object or a string table name.

        :returns QueryBuilder
        """
        return cls._builder(**kwargs).into(table)

    @classmethod
    def with_(cls, table: Selectable, name: str, **kwargs: Any) -> "QueryBuilderType":
        return cls._builder(**kwargs).with_(table, name)

    @classmethod
    def select(cls, *terms: Union[int, float, str, bool, Term], **kwargs: Any) -> "QueryBuilderType":
        """
        Query builder entry point.  Initializes query building without a table and selects fields.  Useful when testing
        SQL functions.

        :param terms:
            Type: list[expression]

            A list of terms to select.  These can be any type of int, float, str, bool, or Term.  They cannot be a Field
            unless the function ``Query.from_`` is called first.

        :returns QueryBuilder
        """
        return cls._builder(**kwargs).select(*terms)

    @classmethod
    def update(cls, table: Union[str, Table], **kwargs) -> "QueryBuilderType":
        """
        Query builder entry point.  Initializes query building and sets the table to update.  When using this
        function, the query becomes an UPDATE query.

        :param table:
            Type: Table or str

            An instance of a Table object or a string table name.

        :returns QueryBuilder
        """
        return cls._builder(**kwargs).update(table)

    @classmethod
    def Table(cls, table_name: str, **kwargs) -> Table[QueryBuilderType]:
        """
        Convenience method for creating a Table that uses this Query class.

        :param table_name:
            Type: str

            A string table name.

        :returns Table
        """
        return Table(table_name, query_cls=cls, **kwargs)

    @classmethod
    def Tables(cls, *names: Union[TypedTuple[str, str], str], **kwargs: Any) -> List["Table[QueryBuilderType]"]:
        """
        Convenience method for creating many tables that uses this Query class.
        See ``Query.make_tables`` for details.

        :param names:
            Type: list[str or tuple]

            A list of string table names, or name and alias tuples.

        :returns Table
        """
        return make_tables(*names, query_cls=cls, **kwargs)


class _SetOperation(Selectable, Term, SQLPart):
    """
    A Query class wrapper for a all set operations, Union DISTINCT or ALL, Intersect, Except or Minus

    Created via the functions `Query.union`,`Query.union_all`,`Query.intersect`, `Query.except_of`,`Query.minus`.

    This class should not be instantiated directly.
    """

    def __init__(
        self,
        base_query: "QueryBuilder",
        set_operation_query: "QueryBuilder",
        set_operation: SetOperation,
        alias: Optional[str] = None,
        wrapper_cls: Type[ValueWrapper] = ValueWrapper,
    ):
        super().__init__(alias)
        self.base_query = base_query
        self._set_operation: List[TypedTuple[SetOperation, QueryBuilder]] = [(set_operation, set_operation_query)]
        self._orderbys: List[TypedTuple[Union[Field, WrappedConstant, None], Optional[Order]]] = []

        self._limit: Optional[int] = None
        self._offset: Optional[int] = None

        self._wrapper_cls = wrapper_cls

    @builder
    def orderby(self, *fields: Union[Field, str], order: Optional[Order] = None):
        field: Union[None, Field, WrappedConstant]
        if fields:
            field_val = fields[-1]
            if isinstance(field_val, str):
                table = self.base_query._from[0]
                if not isinstance(table, Table):
                    raise TypeError(
                        "expect the first \"from\" selectable is table, got {}".format(type(table).__name__)
                    )
                field = Field(field_val, table=table)
            else:
                field = self.base_query.wrap_constant(field_val)
        else:
            field = None
        self._orderbys.append((field, order))

    @builder
    def limit(self, limit: int):
        self._limit = limit

    @builder
    def offset(self, offset: int):
        self._offset = offset

    @builder
    def union(self, other: "QueryBuilder"):
        self._set_operation.append((SetOperation.union, other))

    @builder
    def union_all(self, other: "QueryBuilder"):
        self._set_operation.append((SetOperation.union_all, other))

    @builder
    def intersect(self, other: "QueryBuilder"):
        self._set_operation.append((SetOperation.intersect, other))

    @builder
    def except_of(self, other: "QueryBuilder"):
        self._set_operation.append((SetOperation.except_of, other))

    @builder
    def minus(self, other: "QueryBuilder"):
        self._set_operation.append((SetOperation.minus, other))

    def __add__(self, other: "QueryBuilder") -> "_SetOperation":  # type: ignore
        return self.union(other)

    def __mul__(self, other: "QueryBuilder") -> "_SetOperation":  # type: ignore
        return self.union_all(other)

    def __sub__(self, other: "QueryBuilder") -> "_SetOperation":  # type: ignore
        return self.minus(other)

    def __str__(self) -> str:
        return self.get_sql()

    def get_sql(self, with_alias: bool = False, subquery: bool = False, **kwargs: Any) -> str:
        set_operation_template = " {type} {query_string}"

        kwargs.setdefault("dialect", self.base_query.dialect)
        # This initializes the quote char based on the base query, which could be a dialect specific query class
        # This might be overridden if quote_char is set explicitly in kwargs
        kwargs.setdefault("quote_char", self.base_query.QUOTE_CHAR)

        base_querystring = self.base_query.get_sql(subquery=self.base_query.wrap_set_operation_queries, **kwargs)

        querystring = base_querystring
        for set_operation, set_operation_query in self._set_operation:
            set_operation_querystring = set_operation_query.get_sql(
                subquery=self.base_query.wrap_set_operation_queries, **kwargs
            )

            if len(self.base_query._selects) != len(set_operation_query._selects):
                raise SetOperationException(
                    "Queries must have an equal number of select statements in a set operation."
                    "\n\nMain Query:\n{query1}\n\nSet Operations Query:\n{query2}".format(
                        query1=base_querystring, query2=set_operation_querystring
                    )
                )

            querystring += set_operation_template.format(
                type=set_operation.value, query_string=set_operation_querystring
            )

        if self._orderbys:
            querystring += self._orderby_sql(**kwargs)

        if self._limit is not None:
            querystring += self._limit_sql()

        if self._offset:
            querystring += self._offset_sql()

        if subquery:
            querystring = "({query})".format(query=querystring, **kwargs)

        if with_alias:
            return format_alias_sql(querystring, self.alias or self.get_table_name(), **kwargs)

        return querystring

    def _orderby_sql(self, quote_char: Optional[str] = None, **kwargs: Any) -> str:
        """
        Produces the ORDER BY part of the query.  This is a list of fields and possibly their directionality, ASC or
        DESC. The clauses are stored in the query under self._orderbys as a list of tuples containing the field and
        directionality (which can be None).

        If an order by field is used in the select clause, determined by a matching , then the ORDER BY clause will use
        the alias, otherwise the field will be rendered as SQL.
        """
        clauses = []
        selected_aliases = {s.alias for s in self.base_query._selects if isinstance(s, Term)}
        for field, directionality in self._orderbys:
            term = (
                format_quotes(field.alias, quote_char)  # type: ignore
                if field.alias and (field.alias in selected_aliases)  # type: ignore
                else field.get_sql(quote_char=quote_char, **kwargs)  # type: ignore
            )

            clauses.append(
                "{term} {orient}".format(term=term, orient=directionality.value) if directionality is not None else term
            )

        return " ORDER BY {orderby}".format(orderby=",".join(clauses))

    def _offset_sql(self) -> str:
        return " OFFSET {offset}".format(offset=self._offset)

    def _limit_sql(self) -> str:
        return " LIMIT {limit}".format(limit=self._limit)


class QueryBuilder(Selectable, Term, SQLPart):
    """
    Query Builder is the main class in pypika which stores the state of a query and offers functions which allow the
    state to be branched immutably.
    """

    QUOTE_CHAR: Optional[str] = '"'
    SECONDARY_QUOTE_CHAR: Optional[str] = "'"
    ALIAS_QUOTE_CHAR: Optional[str] = None
    QUERY_ALIAS_QUOTE_CHAR: Optional[str] = None
    QUERY_CLS = Query

    def __init__(
        self,
        dialect: Optional[Dialects] = None,
        wrap_set_operation_queries: bool = True,
        wrapper_cls: Type[ValueWrapper] = ValueWrapper,
        immutable: bool = True,
        as_keyword: bool = False,
    ):
        super().__init__(None)

        self._from: List[Union[Selectable, QueryBuilder, None]] = []
        self._insert_table: Optional[Table] = None
        self._update_table: Optional[Table] = None
        self._delete_from = False
        self._replace = False

        self._with: List[AliasedQuery] = []
        self._selects: List[Term] = []
        self._force_indexes: List[Index] = []
        self._use_indexes: List[Index] = []
        self._columns: List[Term] = []
        self._values: List[Sequence[Union[Term, WrappedConstant]]] = []
        self._distinct = False
        self._ignore = False

        self._for_update = False

        self._wheres: Optional[Union[Term, Criterion]] = None
        self._prewheres: Optional[Criterion] = None
        self._groupbys: List[Union[Term, WrappedConstant]] = []
        self._with_totals = False
        self._havings: Optional[Union[Term, Criterion]] = None
        self._orderbys: List[TypedTuple[WrappedConstant, Optional[Order]]] = []
        self._joins: List[Join] = []
        self._unions: List[None] = []
        self._using: List[Union[Selectable, str]] = []

        self._limit: Optional[int] = None
        self._offset: Optional[int] = None

        self._updates: List[TypedTuple[Field, ValueWrapper]] = []

        self._select_star = False
        self._select_star_tables: Set[Optional[Union[str, Selectable]]] = set()
        self._mysql_rollup = False
        self._select_into = False

        self._subquery_count = 0
        self._foreign_table = False

        self.dialect = dialect
        self.as_keyword = as_keyword
        self.wrap_set_operation_queries = wrap_set_operation_queries

        self._wrapper_cls = wrapper_cls

        self.immutable = immutable

    def __copy__(self) -> Self:
        newone = type(self).__new__(type(self))
        newone.__dict__.update(self.__dict__)
        newone._select_star_tables = copy(self._select_star_tables)
        newone._from = copy(self._from)
        newone._with = copy(self._with)
        newone._selects = copy(self._selects)
        newone._columns = copy(self._columns)
        newone._values = copy(self._values)
        newone._groupbys = copy(self._groupbys)
        newone._orderbys = copy(self._orderbys)
        newone._joins = copy(self._joins)
        newone._unions = copy(self._unions)
        newone._updates = copy(self._updates)
        newone._force_indexes = copy(self._force_indexes)
        newone._use_indexes = copy(self._use_indexes)
        return newone

    @builder
    def from_(self, selectable: Union[Selectable, "QueryBuilder", str]):
        """
        Adds a table to the query. This function can only be called once and will raise an AttributeError if called a
        second time.

        :param selectable:
            Type: ``Table``, ``Query``, or ``str``

            When a ``str`` is passed, a table with the name matching the ``str`` value is used.

        :returns
            A copy of the query with the table added.
        """

        self._from.append(Table(selectable) if isinstance(selectable, str) else selectable)

        if isinstance(selectable, (QueryBuilder, _SetOperation)) and selectable.alias is None:
            if isinstance(selectable, QueryBuilder):
                sub_query_count = selectable._subquery_count
            else:
                sub_query_count = 0

            sub_query_count = max(self._subquery_count, sub_query_count)
            selectable.alias = "sq%d" % sub_query_count
            self._subquery_count = sub_query_count + 1

    @builder
    def replace_table(self, current_table: Optional[Table], new_table: Optional[Table]):
        """
        Replaces all occurrences of the specified table with the new table. Useful when reusing fields across
        queries.

        :param current_table:
            The table instance to be replaces.
        :param new_table:
            The table instance to replace with.
        :return:
            A copy of the query with the tables replaced.
        """
        self._from = [new_table if table == current_table else table for table in self._from]
        self._insert_table = new_table if self._insert_table == current_table else self._insert_table
        self._update_table = new_table if self._update_table == current_table else self._update_table

        self._with = [alias_query.replace_table(current_table, new_table) for alias_query in self._with]
        self._selects = [
            select.replace_table(current_table, new_table) if isinstance(select, Term) else select
            for select in self._selects
        ]
        self._columns = [column.replace_table(current_table, new_table) for column in self._columns]
        self._values = [
            [
                (value.replace_table(current_table, new_table) if isinstance(value, Term) else value)
                for value in value_list
            ]
            for value_list in self._values
        ]

        self._wheres = self._wheres.replace_table(current_table, new_table) if self._wheres else None
        self._prewheres = self._prewheres.replace_table(current_table, new_table) if self._prewheres else None
        self._groupbys = [
            groupby.replace_table(current_table, new_table) if isinstance(groupby, Term) else groupby
            for groupby in self._groupbys
        ]
        self._havings = self._havings.replace_table(current_table, new_table) if self._havings else None
        self._orderbys = [
            (orderby[0].replace_table(current_table, new_table), orderby[1])
            if isinstance(orderby[0], Term)
            else orderby
            for orderby in self._orderbys
        ]
        self._joins = [join.replace_table(current_table, new_table) for join in self._joins]

        if current_table in self._select_star_tables:
            self._select_star_tables.remove(current_table)
            self._select_star_tables.add(new_table)

    @builder
    def with_(self, selectable: Selectable, name: str):
        t = AliasedQuery(name, selectable)
        self._with.append(t)

    @builder
    def into(self, table: Union[str, Table]):
        if self._insert_table is not None:
            raise AttributeError("'Query' object has no attribute '%s'" % "into")

        if self._selects:
            self._select_into = True

        self._insert_table = table if isinstance(table, Table) else Table(table)

    @builder
    def select(self, *terms: Any):
        for term in terms:
            if isinstance(term, Field):
                self._select_field(term)
            elif isinstance(term, str):
                self._select_field_str(term)
            elif isinstance(term, (Function, ArithmeticExpression)):
                self._select_other(term)
            else:
                value = self.wrap_constant(term, wrapper_cls=self._wrapper_cls)
                self._select_other(value)

    @builder
    def delete(self):
        if self._delete_from or self._selects or self._update_table:
            raise AttributeError("'Query' object has no attribute '%s'" % "delete")

        self._delete_from = True

    @builder
    def update(self, table: Union[str, Table]):
        if self._update_table is not None or self._selects or self._delete_from:
            raise AttributeError("'Query' object has no attribute '%s'" % "update")

        self._update_table = table if isinstance(table, Table) else Table(table)

    @builder
    def columns(self, *terms: Union[str, Field, List[Union[str, Field]], TypedTuple[Union[str, Field], ...]]) -> None:
        if self._insert_table is None:
            raise AttributeError("'Query' object has no attribute '%s'" % "insert")

        columns: Iterable[Union[str, Field]]
        if terms and isinstance(terms[0], (list, tuple)):
            columns = terms[0]  # FIXME: should not sliently ignore rest arguments
            # Alternative solution: fix the type comment to tell use here only accepts one sequence.
        else:
            columns = cast(TypedTuple[Union[str, Field]], terms)

        for term in columns:
            if isinstance(term, str):
                term = Field(term, table=self._insert_table)
            self._columns.append(term)

    @builder
    def insert(self, *terms: Any):
        self._apply_terms(*terms)
        self._replace = False

    @builder
    def replace(self, *terms: Any):
        self._apply_terms(*terms)
        self._replace = True

    @builder
    def force_index(self, term: Union[str, Index], *terms: Union[str, Index]):
        for t in (term, *terms):
            if isinstance(t, Index):
                self._force_indexes.append(t)
            elif isinstance(t, str):
                self._force_indexes.append(Index(t))

    @builder
    def use_index(self, term: Union[str, Index], *terms: Union[str, Index]):
        for t in (term, *terms):
            if isinstance(t, Index):
                self._use_indexes.append(t)
            elif isinstance(t, str):
                self._use_indexes.append(Index(t))

    @builder
    def distinct(self):
        self._distinct = True

    @builder
    def for_update(self):
        self._for_update = True

    @builder
    def ignore(self):
        self._ignore = True

    @builder
    def prewhere(self, criterion: Criterion):
        if not self._validate_table(criterion):
            self._foreign_table = True

        if self._prewheres:
            self._prewheres &= criterion
        else:
            self._prewheres = criterion

    @builder
    def where(self, criterion: Union[Term, EmptyCriterion]):
        if isinstance(criterion, EmptyCriterion):
            return

        if not self._validate_table(criterion):
            self._foreign_table = True

        if self._wheres:
            self._wheres &= criterion  # type: ignore
        else:
            self._wheres = criterion

    @builder
    def having(self, criterion: Union[Term, EmptyCriterion]):
        if isinstance(criterion, EmptyCriterion):
            return

        if self._havings:
            self._havings &= criterion  # type: ignore
        else:
            self._havings = criterion

    @builder
    def groupby(self, *terms: Union[str, int, Term]):
        table = self._from[0]
        if not isinstance(table, Selectable):
            raise TypeError("expect table is a Selectable, got {}".format(type(table).__name__))
        for term in terms:
            new_term: Union[WrappedConstant, Field]
            if isinstance(term, str):
                new_term = Field(term, table=table)
            elif isinstance(term, int):
                new_term = Field(str(term), table=table).wrap_constant(term)
            else:
                new_term = term

            self._groupbys.append(new_term)

    @builder
    def with_totals(self):
        self._with_totals = True

    @builder
    def rollup(self, *terms: Union[list, tuple, set, Term], **kwargs: Any):
        for_mysql = "mysql" == kwargs.get("vendor")

        if self._mysql_rollup:
            raise AttributeError("'Query' object has no attribute '%s'" % "rollup")

        wrapped_terms = [Tuple(*term) if isinstance(term, (list, tuple, set)) else term for term in terms]

        if for_mysql:
            # MySQL rolls up all of the dimensions always
            if not wrapped_terms and not self._groupbys:
                raise RollupException(
                    "At least one group is required. Call Query.groupby(term) or pass" "as parameter to rollup."
                )

            self._mysql_rollup = True
            self._groupbys += wrapped_terms

        elif 0 < len(self._groupbys) and isinstance(self._groupbys[-1], Rollup):
            # If a rollup was added last, then append the new terms to the previous rollup
            self._groupbys[-1].args += wrapped_terms

        else:
            self._groupbys.append(Rollup(*wrapped_terms))

    @builder
    def orderby(self, *fields: WrappedConstantValue, order: Optional[Order] = None):
        table = self._from[0]
        if not isinstance(table, Selectable):
            raise TypeError("expect table is a Selectable, got {}".format(type(table).__name__))
        for field in fields:
            target_field = Field(field, table=table) if isinstance(field, str) else self.wrap_constant(field)

            self._orderbys.append((target_field, order))

    @builder
    def join(
        self, item: Union[Table, "QueryBuilder", AliasedQuery, _SetOperation], how: JoinType = JoinType.inner
    ) -> "Joiner[Self]":
        if isinstance(item, Table):
            return Joiner(self, item, how, type_label="table")

        elif isinstance(item, (QueryBuilder, _SetOperation)):
            if item.alias is None:
                self._tag_subquery(item)
            return Joiner(self, item, how, type_label="subquery")

        elif isinstance(item, AliasedQuery):
            return Joiner(self, item, how, type_label="table")

        raise ValueError("Cannot join on type '%s'" % type(item))

    def inner_join(self, item: Union[Table, "QueryBuilder", AliasedQuery]) -> "Joiner[Self]":
        return self.join(item, JoinType.inner)

    def left_join(self, item: Union[Table, "QueryBuilder", AliasedQuery]) -> "Joiner[Self]":
        return self.join(item, JoinType.left)

    def left_outer_join(self, item: Union[Table, "QueryBuilder", AliasedQuery]) -> "Joiner[Self]":
        return self.join(item, JoinType.left_outer)

    def right_join(self, item: Union[Table, "QueryBuilder", AliasedQuery]) -> "Joiner[Self]":
        return self.join(item, JoinType.right)

    def right_outer_join(self, item: Union[Table, "QueryBuilder", AliasedQuery]) -> "Joiner[Self]":
        return self.join(item, JoinType.right_outer)

    def outer_join(self, item: Union[Table, "QueryBuilder", AliasedQuery]) -> "Joiner[Self]":
        return self.join(item, JoinType.outer)

    def full_outer_join(self, item: Union[Table, "QueryBuilder", AliasedQuery]) -> "Joiner[Self]":
        return self.join(item, JoinType.full_outer)

    def cross_join(self, item: Union[Table, "QueryBuilder", AliasedQuery]) -> "Joiner[Self]":
        return self.join(item, JoinType.cross)

    def hash_join(self, item: Union[Table, "QueryBuilder", AliasedQuery]) -> "Joiner[Self]":
        return self.join(item, JoinType.hash)

    @builder
    def limit(self, limit: int):
        self._limit = limit

    @builder
    def offset(self, offset: int):
        self._offset = offset

    @builder
    def union(self, other: "QueryBuilder") -> _SetOperation:
        return _SetOperation(self, other, SetOperation.union, wrapper_cls=self._wrapper_cls)

    @builder
    def union_all(self, other: "QueryBuilder") -> _SetOperation:
        return _SetOperation(self, other, SetOperation.union_all, wrapper_cls=self._wrapper_cls)

    @builder
    def intersect(self, other: "QueryBuilder") -> _SetOperation:
        return _SetOperation(self, other, SetOperation.intersect, wrapper_cls=self._wrapper_cls)

    @builder
    def except_of(self, other: "QueryBuilder") -> _SetOperation:
        return _SetOperation(self, other, SetOperation.except_of, wrapper_cls=self._wrapper_cls)

    @builder
    def minus(self, other: "QueryBuilder") -> _SetOperation:
        return _SetOperation(self, other, SetOperation.minus, wrapper_cls=self._wrapper_cls)

    @builder
    def set(self, field: Union[Field, str], value: Any):
        field = Field(field) if not isinstance(field, Field) else field
        self._updates.append((field, self._wrapper_cls(value)))

    def __add__(self, other: "QueryBuilder") -> _SetOperation:  # type: ignore
        return self.union(other)

    def __mul__(self, other: "QueryBuilder") -> _SetOperation:  # type: ignore
        return self.union_all(other)

    def __sub__(self, other: "QueryBuilder") -> _SetOperation:  # type: ignore
        return self.minus(other)

    @builder
    def slice(self, slice: slice):
        self._offset = slice.start
        self._limit = slice.stop

    @overload
    def __getitem__(self, item: str) -> Field:
        ...

    @overload
    def __getitem__(self, item: builtins.slice) -> Self:
        ...

    def __getitem__(self, item: Union[str, builtins.slice]) -> Union[Self, Field]:
        if not isinstance(item, slice):
            return super().__getitem__(item)
        return self.slice(item)

    @staticmethod
    def _list_aliases(field_set: Sequence[Field], quote_char: Optional[str] = None) -> List[str]:
        return [field.alias or field.get_sql(quote_char=quote_char) for field in field_set]

    def _select_field_str(self, term: str) -> None:
        if 0 == len(self._from):
            raise QueryException("Cannot select {term}, no FROM table specified.".format(term=term))

        if term == "*":
            self._select_star = True
            self._selects = [Star()]
            return
        table = self._from[0]
        if not isinstance(table, Selectable):
            raise TypeError("expect table is a Selectable, got {}".format(type(table).__name__))
        self._select_field(Field(term, table=table))

    def _select_field(self, term: Field) -> None:
        if self._select_star:
            # Do not add select terms after a star is selected
            return

        if term.table in self._select_star_tables:
            # Do not add select terms for table after a table star is selected
            return

        if isinstance(term, Star):
            self._selects = [
                select
                for select in self._selects
                if (not hasattr(select, "table")) or (isinstance(select, Field) and term.table != select.table)
            ]
            self._select_star_tables.add(term.table)

        self._selects.append(term)

    def _select_other(self, function: Term) -> None:
        self._selects.append(function)

    def fields_(self) -> Set[Field]:
        # Don't return anything here. Subqueries have their own fields.
        return set()

    def do_join(self, join: "Join") -> None:
        def _assert_not_none(v):
            if v is not None:
                return v
            else:
                raise TypeError("expect Selectable, got None")

        base_tables = tuple(
            map(
                _assert_not_none,
                chain(self._from, (self._update_table,) if self._update_table else tuple(), self._with),
            )
        )
        join.validate(base_tables, self._joins)

        table_in_query = reduce(
            operator.add,
            (clause._table_name == join.item._table_name for clause in base_tables if isinstance(clause, Table)),
            0,
        )
        if isinstance(join.item, Table) and (join.item.alias is None) and (table_in_query > 0):
            # On the odd chance that we join the same table as the FROM table and don't set an alias
            # FIXME only works once
            join.item.alias = join.item._table_name + "2"

        self._joins.append(join)

    def is_joined(self, table: Table) -> bool:
        return any(table == join.item for join in self._joins)

    def _validate_table(self, term: Term) -> bool:
        """
        Returns False if the term references a table not already part of the
        FROM clause or JOINS and True otherwise.
        """
        base_tables = self._from + [self._update_table]

        for field in term.fields_():
            table_in_base_tables = field.table in base_tables
            table_in_joins = field.table in [join.item for join in self._joins]
            if all(
                [
                    field.table is not None,
                    not table_in_base_tables,
                    not table_in_joins,
                    field.table != self._update_table,
                ]
            ):
                return False
        return True

    def _tag_subquery(self, subquery: Union["QueryBuilder", _SetOperation]) -> None:
        subquery.alias = "sq%d" % self._subquery_count
        self._subquery_count += 1

    def _apply_terms(self, *terms: Any) -> None:
        """
        Handy function for INSERT and REPLACE statements in order to check if
        terms are introduced and how append them to `self._values`
        """
        if self._insert_table is None:
            raise AttributeError("'Query' object has no attribute '%s'" % "insert")

        if not terms:
            return

        if not isinstance(terms[0], (list, tuple, set)):
            terms = (terms,)

        for values in terms:
            self._values.append([(value if isinstance(value, Term) else self.wrap_constant(value)) for value in values])

    def __str__(self) -> str:
        return self.get_sql(dialect=self.dialect)

    def __repr__(self) -> str:
        return self.__str__()

    def __eq__(self, other: Any) -> bool:  # type: ignore
        if not isinstance(other, QueryBuilder):
            return False

        if not self.alias == other.alias:
            return False

        return True

    def __ne__(self, other: Any) -> bool:  # type: ignore
        return not self.__eq__(other)

    def __hash__(self) -> int:
        return hash(self.alias) + sum(hash(clause) for clause in self._from)

    def _set_kwargs_defaults(self, kwargs: dict) -> None:
        kwargs.setdefault("quote_char", self.QUOTE_CHAR)
        kwargs.setdefault("secondary_quote_char", self.SECONDARY_QUOTE_CHAR)
        kwargs.setdefault("alias_quote_char", self.ALIAS_QUOTE_CHAR)
        kwargs.setdefault("as_keyword", self.as_keyword)
        kwargs.setdefault("dialect", self.dialect)

    def get_sql(self, with_alias: bool = False, subquery: bool = False, **kwargs: Any) -> str:
        self._set_kwargs_defaults(kwargs)
        if not (self._selects or self._insert_table or self._delete_from or self._update_table):
            return ""
        if self._insert_table and not (self._selects or self._values):
            return ""
        if self._update_table and not self._updates:
            return ""

        has_joins = bool(self._joins)
        has_multiple_from_clauses = 1 < len(self._from)
        has_subquery_from_clause = 0 < len(self._from) and isinstance(self._from[0], QueryBuilder)
        has_reference_to_foreign_table = self._foreign_table
        has_update_from = self._update_table and self._from

        kwargs["with_namespace"] = any(
            [
                has_joins,
                has_multiple_from_clauses,
                has_subquery_from_clause,
                has_reference_to_foreign_table,
                has_update_from,
            ]
        )

        if self._update_table:
            if self._with:
                querystring = self._with_sql(**kwargs)
            else:
                querystring = ""

            querystring += self._update_sql(**kwargs)

            if self._joins:
                querystring += " " + " ".join(join.get_sql(**kwargs) for join in self._joins)

            querystring += self._set_sql(**kwargs)

            if self._from:
                querystring += self._from_sql(**kwargs)

            if self._wheres:
                querystring += self._where_sql(**kwargs)

            if self._limit is not None:
                querystring += self._limit_sql()

            return querystring

        if self._delete_from:
            querystring = self._delete_sql(**kwargs)

        elif not self._select_into and self._insert_table:
            if self._with:
                querystring = self._with_sql(**kwargs)
            else:
                querystring = ""

            if self._replace:
                querystring += self._replace_sql(**kwargs)
            else:
                querystring += self._insert_sql(**kwargs)

            if self._columns:
                querystring += self._columns_sql(**kwargs)

            if self._values:
                querystring += self._values_sql(**kwargs)
                return querystring
            else:
                querystring += " " + self._select_sql(**kwargs)

        else:
            if self._with:
                querystring = self._with_sql(**kwargs)
            else:
                querystring = ""

            querystring += self._select_sql(**kwargs)

            if self._insert_table:
                querystring += self._into_sql(**kwargs)

        if self._from:
            querystring += self._from_sql(**kwargs)

        if self._using:
            querystring += self._using_sql(**kwargs)

        if self._force_indexes:
            querystring += self._force_index_sql(**kwargs)

        if self._use_indexes:
            querystring += self._use_index_sql(**kwargs)

        if self._joins:
            querystring += " " + " ".join(join.get_sql(**kwargs) for join in self._joins)

        if self._prewheres:
            querystring += self._prewhere_sql(**kwargs)

        if self._wheres:
            querystring += self._where_sql(**kwargs)

        if self._groupbys:
            querystring += self._group_sql(**kwargs)
            if self._mysql_rollup:
                querystring += self._rollup_sql()

        if self._havings:
            querystring += self._having_sql(**kwargs)

        if self._orderbys:
            querystring += self._orderby_sql(**kwargs)

        querystring = self._apply_pagination(querystring)

        if self._for_update:
            querystring += self._for_update_sql(**kwargs)

        if subquery:
            querystring = "({query})".format(query=querystring)

        if with_alias:
            kwargs['alias_quote_char'] = (
                self.ALIAS_QUOTE_CHAR if self.QUERY_ALIAS_QUOTE_CHAR is None else self.QUERY_ALIAS_QUOTE_CHAR
            )
            return format_alias_sql(querystring, self.alias, **kwargs)

        return querystring

    def _apply_pagination(self, querystring: str) -> str:
        if self._limit is not None:
            querystring += self._limit_sql()

        if self._offset:
            querystring += self._offset_sql()

        return querystring

    def _with_sql(self, **kwargs: Any) -> str:
        return "WITH " + ",".join(
            clause.name + " AS (" + clause.get_sql(subquery=False, with_alias=False, **kwargs) + ") "
            for clause in self._with
        )

    def _distinct_sql(self, **kwargs: Any) -> str:
        if self._distinct:
            distinct = 'DISTINCT '
        else:
            distinct = ''

        return distinct

    def _for_update_sql(self, **kwargs) -> str:
        if self._for_update:
            for_update = ' FOR UPDATE'
        else:
            for_update = ''

        return for_update

    def _select_sql(self, **kwargs: Any) -> str:
        return "SELECT {distinct}{select}".format(
            distinct=self._distinct_sql(**kwargs),
            select=",".join(term.get_sql(with_alias=True, subquery=True, **kwargs) for term in self._selects),
        )

    def _insert_sql(self, **kwargs: Any) -> str:
        assert self._insert_table is not None
        return "INSERT {ignore}INTO {table}".format(
            table=self._insert_table.get_sql(**kwargs),
            ignore="IGNORE " if self._ignore else "",
        )

    def _replace_sql(self, **kwargs: Any) -> str:
        assert self._insert_table is not None
        return "REPLACE INTO {table}".format(
            table=self._insert_table.get_sql(**kwargs),
        )

    @staticmethod
    def _delete_sql(**kwargs: Any) -> str:
        return "DELETE"

    def _update_sql(self, **kwargs: Any) -> str:
        assert self._update_table is not None
        return "UPDATE {table}".format(table=self._update_table.get_sql(**kwargs))

    def _columns_sql(self, with_namespace: bool = False, **kwargs: Any) -> str:
        """
        SQL for Columns clause for INSERT queries
        :param with_namespace:
            Remove from kwargs, never format the column terms with namespaces since only one table can be inserted into
        """
        return " ({columns})".format(
            columns=",".join(term.get_sql(with_namespace=False, **kwargs) for term in self._columns)
        )

    @classmethod
    def _assert_type_fn(cls, klass: Type[_T]) -> Callable[[Any], _T]:
        def _assert_type(val: Any):
            assert isinstance(val, klass)
            return val

        return _assert_type

    def _values_sql(self, **kwargs: Any) -> str:
        return " VALUES ({values})".format(
            values="),(".join(
                ",".join(
                    term.get_sql(with_alias=True, subquery=True, **kwargs)
                    for term in map(self._assert_type_fn(Term), row)
                )
                for row in self._values
            )
        )

    def _into_sql(self, **kwargs: Any) -> str:
        assert self._insert_table is not None
        return " INTO {table}".format(
            table=self._insert_table.get_sql(with_alias=False, **kwargs),
        )

    def _from_sql(self, with_namespace: bool = False, **kwargs: Any) -> str:
        return " FROM {selectable}".format(
            selectable=",".join(clause.get_sql(subquery=True, with_alias=True, **kwargs) for clause in self._from)  # type: ignore
        )

    def _using_sql(self, with_namespace: bool = False, **kwargs: Any) -> str:
        return " USING {selectable}".format(
            selectable=",".join(
                clause.get_sql(subquery=True, with_alias=True, **kwargs) if isinstance(clause, SQLPart) else clause
                for clause in self._using
            )
        )

    def _force_index_sql(self, **kwargs: Any) -> str:
        return " FORCE INDEX ({indexes})".format(
            indexes=",".join(index.get_sql(**kwargs) for index in self._force_indexes),
        )

    def _use_index_sql(self, **kwargs: Any) -> str:
        return " USE INDEX ({indexes})".format(
            indexes=",".join(index.get_sql(**kwargs) for index in self._use_indexes),
        )

    def _prewhere_sql(self, quote_char: Optional[str] = None, **kwargs: Any) -> str:
        assert self._prewheres is not None
        return " PREWHERE {prewhere}".format(
            prewhere=self._prewheres.get_sql(quote_char=quote_char, subquery=True, **kwargs)
        )

    def _where_sql(self, quote_char: Optional[str] = None, **kwargs: Any) -> str:
        assert self._wheres is not None
        return " WHERE {where}".format(where=self._wheres.get_sql(quote_char=quote_char, subquery=True, **kwargs))

    def _group_sql(
        self,
        quote_char: Optional[str] = None,
        alias_quote_char: Optional[str] = None,
        groupby_alias: bool = True,
        **kwargs: Any,
    ) -> str:
        """
        Produces the GROUP BY part of the query.  This is a list of fields. The clauses are stored in the query under
        self._groupbys as a list fields.

        If an groupby field is used in the select clause,
        determined by a matching alias, and the groupby_alias is set True
        then the GROUP BY clause will use the alias,
        otherwise the entire field will be rendered as SQL.
        """
        clauses = []
        selected_aliases = {s.alias for s in self._selects}
        for field in self._groupbys:
            assert isinstance(field, Term)
            if groupby_alias and field.alias and (field.alias in selected_aliases):
                clauses.append(format_quotes(field.alias, alias_quote_char or quote_char))
            else:
                clauses.append(field.get_sql(quote_char=quote_char, alias_quote_char=alias_quote_char, **kwargs))

        sql = " GROUP BY {groupby}".format(groupby=",".join(clauses))

        if self._with_totals:
            return sql + " WITH TOTALS"

        return sql

    def _orderby_sql(
        self,
        quote_char: Optional[str] = None,
        alias_quote_char: Optional[str] = None,
        orderby_alias: bool = True,
        **kwargs: Any,
    ) -> str:
        """
        Produces the ORDER BY part of the query.  This is a list of fields and possibly their directionality, ASC or
        DESC. The clauses are stored in the query under self._orderbys as a list of tuples containing the field and
        directionality (which can be None).

        If an order by field is used in the select clause,
        determined by a matching, and the orderby_alias
        is set True then the ORDER BY clause will use
        the alias, otherwise the field will be rendered as SQL.
        """
        clauses = []
        selected_aliases = {s.alias for s in self._selects}
        for field, directionality in self._orderbys:
            assert isinstance(field, Term)
            term = (
                format_quotes(field.alias, alias_quote_char or quote_char)
                if orderby_alias and field.alias and field.alias in selected_aliases
                else field.get_sql(quote_char=quote_char, alias_quote_char=alias_quote_char, **kwargs)
            )

            clauses.append(
                "{term} {orient}".format(term=term, orient=directionality.value) if directionality is not None else term
            )

        return " ORDER BY {orderby}".format(orderby=",".join(clauses))

    def _rollup_sql(self) -> str:
        return " WITH ROLLUP"

    def _having_sql(self, quote_char: Optional[str] = None, **kwargs: Any) -> str:
        return " HAVING {having}".format(having=self._havings.get_sql(quote_char=quote_char, **kwargs))  # type: ignore

    def _offset_sql(self) -> str:
        return " OFFSET {offset}".format(offset=self._offset)

    def _limit_sql(self) -> str:
        return " LIMIT {limit}".format(limit=self._limit)

    def _set_sql(self, **kwargs: Any) -> str:
        return " SET {set}".format(
            set=",".join(
                "{field}={value}".format(
                    field=field.get_sql(**dict(kwargs, with_namespace=False)), value=value.get_sql(**kwargs)
                )
                for field, value in self._updates
            )
        )


JoinableTerm = Union[Table, "QueryBuilder", AliasedQuery, _SetOperation]


class Joiner(Generic[QueryBuilderType]):
    def __init__(self, query: "QueryBuilderType", item: JoinableTerm, how: JoinType, type_label: str) -> None:
        self.query = query
        self.item = item
        self.how = how
        self.type_label = type_label

    def on(self, criterion: Optional[Criterion], collate: Optional[str] = None) -> "QueryBuilderType":
        if criterion is None:
            raise JoinException(
                "Parameter 'criterion' is required for a "
                "{type} JOIN but was not supplied.".format(type=self.type_label)
            )

        self.query.do_join(JoinOn(self.item, self.how, criterion, collate))
        return self.query

    def on_field(self, *fields: Any) -> "QueryBuilderType":
        if not fields:
            raise JoinException(
                "Parameter 'fields' is required for a " "{type} JOIN but was not supplied.".format(type=self.type_label)
            )

        criterion: Optional[Criterion] = None
        for field in fields:
            consituent = Field(field, table=self.query._from[0]) == Field(field, table=self.item)
            criterion = (criterion & consituent) if (criterion is not None) else consituent

        self.query.do_join(JoinOn(self.item, self.how, cast(Criterion, criterion)))
        return self.query

    def using(self, *fields: Any) -> "QueryBuilderType":
        if not fields:
            raise JoinException("Parameter 'fields' is required when joining with a using clause but was not supplied.")

        self.query.do_join(JoinUsing(self.item, self.how, [Field(field) for field in fields]))
        return self.query

    def cross(self) -> "QueryBuilderType":
        """Return cross join"""
        self.query.do_join(Join(self.item, JoinType.cross))

        return self.query


class Join(SQLPart):
    def __init__(self, item: JoinableTerm, how: JoinType) -> None:
        self.item = item
        self.how = how

    def get_sql(self, **kwargs: Any) -> str:
        sql = "JOIN {table}".format(
            table=self.item.get_sql(subquery=True, with_alias=True, **kwargs),
        )

        if self.how.value:
            return "{type} {join}".format(join=sql, type=self.how.value)
        return sql

    def validate(self, _from: Iterable[Selectable], _joins: Iterable["Join"]) -> None:
        pass

    @builder
    def replace_table(self, current_table: Optional[Table], new_table: Optional[Table]):
        """
        Replaces all occurrences of the specified table with the new table. Useful when reusing
        fields across queries.

        :param current_table:
            The table to be replaced.
        :param new_table:
            The table to replace with.
        :return:
            A copy of the join with the tables replaced.
        """
        self.item = self.item.replace_table(current_table, new_table)


class JoinOn(Join):
    def __init__(self, item: JoinableTerm, how: JoinType, criteria: Criterion, collate: Optional[str] = None) -> None:
        super().__init__(item, how)
        self.criterion = criteria
        self.collate = collate

    def get_sql(self, **kwargs: Any) -> str:
        join_sql = super().get_sql(**kwargs)
        return "{join} ON {criterion}{collate}".format(
            join=join_sql,
            criterion=self.criterion.get_sql(subquery=True, **kwargs),
            collate=" COLLATE {}".format(self.collate) if self.collate else "",
        )

    def validate(self, _from: Iterable[Selectable], _joins: Iterable[Join]) -> None:
        criterion_tables = set([f.table for f in self.criterion.fields_()])
        available_tables = set(_from) | {join.item for join in _joins} | {self.item}
        missing_tables = criterion_tables - available_tables
        if missing_tables:
            raise JoinException(
                "Invalid join criterion. One field is required from the joined item and "
                "another from the selected table or an existing join.  Found [{tables}]".format(
                    tables=", ".join(map(str, missing_tables))
                )
            )

    @builder
    def replace_table(self, current_table: Optional[Table], new_table: Optional[Table]):
        """
        Replaces all occurrences of the specified table with the new table. Useful when reusing
        fields across queries.

        :param current_table:
            The table to be replaced.
        :param new_table:
            The table to replace with.
        :return:
            A copy of the join with the tables replaced.
        """
        if new_table is not None:
            self.item = new_table if self.item == current_table else self.item
            self.criterion = self.criterion.replace_table(current_table, new_table)
        else:
            raise ValueError("new_table should not be None for {}".format(type(self).__name__))


class JoinUsing(Join):
    def __init__(self, item: JoinableTerm, how: JoinType, fields: Sequence[Field]) -> None:
        super().__init__(item, how)
        self.fields = fields

    def get_sql(self, **kwargs: Any) -> str:
        join_sql = super().get_sql(**kwargs)
        return "{join} USING ({fields})".format(
            join=join_sql,
            fields=",".join(field.get_sql(**kwargs) for field in self.fields),
        )

    def validate(self, _from: Iterable[Selectable], _joins: Iterable[Join]) -> None:
        pass

    @builder
    def replace_table(self, current_table: Optional[Table], new_table: Optional[Table]):
        """
        Replaces all occurrences of the specified table with the new table. Useful when reusing
        fields across queries.

        :param current_table:
            The table to be replaced.
        :param new_table:
            The table to replace with.
        :return:
            A copy of the join with the tables replaced.
        """
        if new_table is not None:
            self.item = new_table if self.item == current_table else self.item
            self.fields = [field.replace_table(current_table, new_table) for field in self.fields]
        else:
            raise ValueError("new_table should not be None for {}".format(type(self).__name__))


class CreateQueryBuilder(SQLPart):
    """
    Query builder used to build CREATE queries.
    """

    QUOTE_CHAR: Optional[str] = '"'
    SECONDARY_QUOTE_CHAR: Optional[str] = "'"
    ALIAS_QUOTE_CHAR: Optional[str] = None
    QUERY_CLS = Query

    def __init__(self, dialect: Optional[Dialects] = None) -> None:
        self._create_table: Optional[Table] = None
        self._temporary = False
        self._unlogged = False
        self._as_select: Optional[QueryBuilder] = None
        self._columns: List[Column] = []
        self._period_fors: List[PeriodFor] = []
        self._with_system_versioning = False
        self._primary_key: Optional[List[Column]] = []
        self._uniques: List[Iterable[Column]] = []
        self._if_not_exists = False
        self.dialect = dialect
        self._foreign_key: Optional[List[Column]] = None
        self._foreign_key_reference_table: Optional[Union[Table, str]] = None
        self._foreign_key_reference: Optional[List[Column]] = None
        self._foreign_key_on_update: Optional[ReferenceOption] = None
        self._foreign_key_on_delete: Optional[ReferenceOption] = None

    def _set_kwargs_defaults(self, kwargs: dict) -> None:
        kwargs.setdefault("quote_char", self.QUOTE_CHAR)
        kwargs.setdefault("secondary_quote_char", self.SECONDARY_QUOTE_CHAR)
        kwargs.setdefault("dialect", self.dialect)

    @builder
    def create_table(self, table: Union[Table, str]):
        """
        Creates the table.

        :param table:
            An instance of a Table object or a string table name.

        :raises AttributeError:
            If the table is already created.

        :return:
            CreateQueryBuilder.
        """
        if self._create_table:
            raise AttributeError("'Query' object already has attribute create_table")

        self._create_table = table if isinstance(table, Table) else Table(table)

    @builder
    def temporary(self):
        """
        Makes the table temporary.

        :return:
            CreateQueryBuilder.
        """
        self._temporary = True

    @builder
    def unlogged(self):
        """
        Makes the table unlogged.

        :return:
            CreateQueryBuilder.
        """
        self._unlogged = True

    @builder
    def with_system_versioning(self):
        """
        Adds system versioning.

        :return:
            CreateQueryBuilder.
        """
        self._with_system_versioning = True

    @builder
    def columns(self, *columns: Union[str, TypedTuple[str, str], Column]):
        """
        Adds the columns.

        :param columns:
            Type:  Union[str, TypedTuple[str, str], Column]

            A list of columns.

        :raises AttributeError:
            If the table is an as_select table.

        :return:
            CreateQueryBuilder.
        """
        if self._as_select:
            raise AttributeError("'Query' object already has attribute as_select")

        for column in columns:
            if isinstance(column, str):
                column = Column(column)
            elif isinstance(column, tuple):
                column = Column(column_name=column[0], column_type=column[1])
            self._columns.append(column)

    @builder
    def period_for(self, name, start_column: Union[str, Column], end_column: Union[str, Column]):
        """
        Adds a PERIOD FOR clause.

        :param name:
            The period name.

        :param start_column:
            The column that starts the period.

        :param end_column:
            The column that ends the period.

        :return:
            CreateQueryBuilder.
        """
        self._period_fors.append(PeriodFor(name, start_column, end_column))

    @builder
    def unique(self, *columns: Union[str, Column]):
        """
        Adds a UNIQUE constraint.

        :param columns:
            Type:  Union[str, Column]

            A list of columns.

        :return:
            CreateQueryBuilder.
        """
        self._uniques.append(self._prepare_columns_input(columns))

    @builder
    def primary_key(self, *columns: Union[str, Column]):
        """
        Adds a primary key constraint.

        :param columns:
            Type:  Union[str, Column]

            A list of columns.

        :raises AttributeError:
            If the primary key is already defined.

        :return:
            CreateQueryBuilder.
        """
        if self._primary_key:
            raise AttributeError("'Query' object already has attribute primary_key")
        self._primary_key = self._prepare_columns_input(columns)

    @builder
    def foreign_key(
        self,
        columns: List[Union[str, Column]],
        reference_table: Union[str, Table],
        reference_columns: List[Union[str, Column]],
        on_delete: Optional[ReferenceOption] = None,
        on_update: Optional[ReferenceOption] = None,
    ):
        """
        Adds a foreign key constraint.

        :param columns:
            Type:  List[Union[str, Column]]

            A list of foreign key columns.

        :param reference_table:
            Type: Union[str, Table]

            The parent table name.

        :param reference_columns:
            Type: List[Union[str, Column]]

            Parent key columns.

        :param on_delete:
            Type: ReferenceOption

            Delete action.

        :param on_update:
            Type: ReferenceOption

            Update option.

        :raises AttributeError:
            If the foreign key is already defined.

        :return:
            CreateQueryBuilder.
        """
        if self._foreign_key:
            raise AttributeError("'Query' object already has attribute foreign_key")
        self._foreign_key = self._prepare_columns_input(columns)
        self._foreign_key_reference_table = reference_table
        self._foreign_key_reference = self._prepare_columns_input(reference_columns)
        self._foreign_key_on_delete = on_delete
        self._foreign_key_on_update = on_update

    @builder
    def as_select(self, query_builder: QueryBuilder):
        """
        Creates the table from a select statement.

        :param query_builder:
            The query.

        :raises AttributeError:
            If columns have been defined for the table.

        :return:
            CreateQueryBuilder.
        """
        if self._columns:
            raise AttributeError("'Query' object already has attribute columns")

        if not isinstance(query_builder, QueryBuilder):
            raise TypeError("Expected 'item' to be instance of QueryBuilder")

        self._as_select = query_builder

    @builder
    def if_not_exists(self):
        self._if_not_exists = True

    def get_sql(self, **kwargs: Any) -> str:
        """
        Gets the sql statement string.

        :return: The create table statement.
        :rtype: str
        """
        self._set_kwargs_defaults(kwargs)

        if not self._create_table:
            return ""

        if not self._columns and not self._as_select:
            return ""

        create_table = self._create_table_sql(**kwargs)

        if self._as_select:
            return create_table + self._as_select_sql(**kwargs)

        body = self._body_sql(**kwargs)
        table_options = self._table_options_sql(**kwargs)

        return "{create_table} ({body}){table_options}".format(
            create_table=create_table, body=body, table_options=table_options
        )

    def _create_table_sql(self, **kwargs: Any) -> str:
        table_type = ''
        if self._temporary:
            table_type = 'TEMPORARY '
        elif self._unlogged:
            table_type = 'UNLOGGED '

        if_not_exists = ''
        if self._if_not_exists:
            if_not_exists = 'IF NOT EXISTS '

        return "CREATE {table_type}TABLE {if_not_exists}{table}".format(
            table_type=table_type,
            if_not_exists=if_not_exists,
            table=self._create_table.get_sql(**kwargs),  # type: ignore
        )

    def _table_options_sql(self, **kwargs) -> str:
        table_options = ""

        if self._with_system_versioning:
            table_options += ' WITH SYSTEM VERSIONING'

        return table_options

    def _column_clauses(self, **kwargs) -> List[str]:
        return [column.get_sql(**kwargs) for column in self._columns]

    def _period_for_clauses(self, **kwargs) -> List[str]:
        return [period_for.get_sql(**kwargs) for period_for in self._period_fors]

    def _unique_key_clauses(self, **kwargs) -> List[str]:
        return [
            "UNIQUE ({unique})".format(unique=",".join(column.get_name_sql(**kwargs) for column in unique))
            for unique in self._uniques
        ]

    def _primary_key_clause(self, **kwargs) -> str:
        return "PRIMARY KEY ({columns})".format(
            columns=",".join(column.get_name_sql(**kwargs) for column in self._primary_key)  # type: ignore
        )

    def _foreign_key_clause(self, **kwargs) -> str:
        assert self._foreign_key_reference_table is not None
        clause = "FOREIGN KEY ({columns}) REFERENCES {table_name} ({reference_columns})".format(
            columns=",".join(column.get_name_sql(**kwargs) for column in self._foreign_key),  # type: ignore
            table_name=(
                self._foreign_key_reference_table.get_sql(**kwargs)
                if isinstance(self._foreign_key_reference_table, Table)
                else Table(self._foreign_key_reference_table).get_sql()
            ),  # type: ignore
            reference_columns=",".join(column.get_name_sql(**kwargs) for column in self._foreign_key_reference),  # type: ignore
        )
        if self._foreign_key_on_delete:
            clause += " ON DELETE " + self._foreign_key_on_delete.value
        if self._foreign_key_on_update:
            clause += " ON UPDATE " + self._foreign_key_on_update.value

        return clause

    def _body_sql(self, **kwargs) -> str:
        clauses = self._column_clauses(**kwargs)
        clauses += self._period_for_clauses(**kwargs)
        clauses += self._unique_key_clauses(**kwargs)

        if self._primary_key:
            clauses.append(self._primary_key_clause(**kwargs))
        if self._foreign_key:
            clauses.append(self._foreign_key_clause(**kwargs))

        return ",".join(clauses)

    def _as_select_sql(self, **kwargs: Any) -> str:
        return " AS ({query})".format(
            query=self._as_select.get_sql(**kwargs),  # type: ignore
        )

    def _prepare_columns_input(self, columns: Iterable[Union[str, Column]]) -> List[Column]:
        return [(column if isinstance(column, Column) else Column(column)) for column in columns]

    def __str__(self) -> str:
        return self.get_sql()

    def __repr__(self) -> str:
        return self.__str__()


class DropQueryBuilder(SQLPart):
    """
    Query builder used to build DROP queries.
    """

    QUOTE_CHAR: Optional[str] = '"'
    SECONDARY_QUOTE_CHAR: Optional[str] = "'"
    ALIAS_QUOTE_CHAR: Optional[str] = None
    QUERY_CLS = Query

    def __init__(self, dialect: Optional[Dialects] = None) -> None:
        self._drop_target_kind: Optional[str] = None
        self._drop_target: Union[Database, Table, str] = ""
        self._if_exists = None
        self.dialect = dialect

    def _set_kwargs_defaults(self, kwargs: dict) -> None:
        kwargs.setdefault("quote_char", self.QUOTE_CHAR)
        kwargs.setdefault("secondary_quote_char", self.SECONDARY_QUOTE_CHAR)
        kwargs.setdefault("dialect", self.dialect)

    @builder
    def drop_database(self, database: Union[Database, str]):
        target = database if isinstance(database, Database) else Database(database)
        self._set_target('DATABASE', target)

    @builder
    def drop_table(self, table: Union[Table, str]):
        target = table if isinstance(table, Table) else Table(table)
        self._set_target('TABLE', target)

    @builder
    def drop_user(self, user: str):
        self._set_target('USER', user)

    @builder
    def drop_view(self, view: str):
        self._set_target('VIEW', view)

    @builder
    def if_exists(self):
        self._if_exists = True

    def _set_target(self, kind: str, target: Union[Database, Table, str]) -> None:
        if self._drop_target:
            raise AttributeError("'DropQuery' object already has attribute drop_target")
        self._drop_target_kind = kind
        self._drop_target = target

    def get_sql(self, **kwargs: Any) -> str:
        self._set_kwargs_defaults(kwargs)

        if_exists = 'IF EXISTS ' if self._if_exists else ''
        target_name: str = ""

        if isinstance(self._drop_target, Database):
            target_name = self._drop_target.get_sql(**kwargs)
        elif isinstance(self._drop_target, Table):
            target_name = self._drop_target.get_sql(**kwargs)
        else:
            target_name = format_quotes(self._drop_target, self.QUOTE_CHAR)

        return "DROP {kind} {if_exists}{name}".format(
            kind=self._drop_target_kind, if_exists=if_exists, name=target_name
        )

    def __str__(self) -> str:
        return self.get_sql()

    def __repr__(self) -> str:
        return self.__str__()
