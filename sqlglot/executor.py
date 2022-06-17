import ast
import csv
import datetime
import gzip
import logging
import re
import statistics
import time
from collections import deque

import sqlglot.expressions as exp
from sqlglot import parse_one
from sqlglot.optimizer import optimize
from sqlglot import planner
from sqlglot.dialects import Dialect


logger = logging.getLogger("sqlglot")


class reverse_key:
    def __init__(self, obj):
        self.obj = obj

    def __eq__(self, other):
        return other.obj == self.obj

    def __lt__(self, other):
        return other.obj < self.obj


ENV = {
    "__builtins__": {},
    "datetime": datetime,
    "re": re,
    "float": float,
    "int": int,
    "str": str,
    "desc": reverse_key,
    "SUM": sum,
    "AVG": statistics.fmean if hasattr(statistics, "fmean") else statistics.mean,
    "COUNT": lambda acc: sum(1 for e in acc if e is not None),
    "MAX": max,
    "MIN": min,
    "POW": pow,
}


def execute(sql, schema, read=None):
    """
    Run a sql query against data.

    Args:
        sql (str): a sql statement
        schema (dict|sqlglot.optimizer.Schema): database schema.
            This can either be an instance of `sqlglot.optimizer.Schema` or a mapping in one of
            the following forms:
                1. {table: {col: type}}
                2. {db: {table: {col: type}}}
                3. {catalog: {db: {table: {col: type}}}}
        read (str): the SQL dialect to apply during parsing
            (eg. "spark", "hive", "presto", "mysql").
    Returns:
        sqlglot.executor.DataTable: Simple columnar data structure.
    """
    expression = parse_one(sql, read=read)
    expression = optimize(expression, schema)
    logger.debug("Optimized SQL: %s", expression.sql(pretty=True))
    plan = planner.Plan(expression)
    print(plan.root)
    logger.debug("Logical Plan: %s", plan)
    now = time.time()
    result = execute_plan(plan)
    logger.debug("Query finished: %f", time.time() - now)
    return result


def execute_plan(plan, env=None):
    env = env or ENV.copy()

    running = set()
    queue = deque(plan.leaves)
    contexts = {}

    while queue:
        node = queue.popleft()
        context = Context(
            {
                name: data_table
                for dep in node.dependencies
                for name, data_table in contexts[dep].data_tables.items()
            },
            env=env,
        )
        running.add(node)

        if isinstance(node, planner.Scan):
            contexts[node] = scan(node, context)
        elif isinstance(node, planner.Aggregate):
            contexts[node] = aggregate(node, context)
        elif isinstance(node, planner.Join):
            contexts[node] = join(node, context)
        elif isinstance(node, planner.Sort):
            contexts[node] = sort(node, context)
        else:
            raise NotImplementedError

        for dep in node.dependents:
            if dep not in running and all(d in contexts for d in dep.dependencies):
                queue.append(dep)

    root = plan.root
    return contexts[root].data_tables[root.name]


def generate(expression):
    """Convert a SQL expression into literal Python code and compile it into bytecode."""
    sql = PYTHON_GENERATOR.generate(expression)
    return compile(sql, sql, "eval", optimize=2)


def scan(step, context):
    table = step.source.name

    sink = None
    filter_code = generate(step.filter) if step.filter else None
    projections = tuple(generate(expression) for expression in step.projections)

    if table in context:
        if not projections:
            return Context({step.name: context.data_tables[table]})

        table_iter = context.iter_table(table)
    else:
        table_iter = scan_csv(table)

    sink = None

    if step.projections:
        sink = DataTable(
            expression.alias_or_name for expression in step.projections
        )

    for ctx in table_iter:
        if filter_code and not ctx.eval(filter_code):
            continue

        if not sink and not projections:
            sink = DataTable(list(ctx[table].columns))

        if projections:
            sink.add(tuple(ctx.eval(code) for code in projections))
        else:
            sink.add(ctx[table].tuple())

        if step.limit and sink.length >= step.limit:
            break
    return Context({step.name: sink})


def scan_csv(table):
    # pylint: disable=stop-iteration-return
    with gzip.open(f"tests/fixtures/optimizer/tpc-h/{table}.csv.gz", "rt") as f:
        reader = csv.reader(f, delimiter="|")
        columns = next(reader)
        row = next(reader)

        types = []

        for v in row:
            try:
                types.append(type(ast.literal_eval(v)))
            except (ValueError, SyntaxError):
                types.append(str)

        f.seek(0)
        next(reader)

        context = Context({table: columns})

        for row in reader:
            context.set_row(table, tuple(t(v) for t, v in zip(types, row)))
            yield context


def join(step, context):
    source = step.name

    join_context = Context({source: context.data_tables[source]})

    for name, join in step.joins.items():
        join_context = Context({**join_context.data_tables, name: context.data_tables[name]})
        kind = join["kind"]

        if kind == "CROSS":
            sink = nested_loop_join(join, source, name, join_context)
        else:
            sink = sort_merge_join(join, source, name, join_context)

        join_context = Context({name: sink for name in join_context.data_tables})

    projections = tuple(generate(expression) for expression in step.projections)

    if projections:
        sink = DataTable(
            expression.alias_or_name for expression in step.projections
        )
        for ctx in join_context.iter_table(source):
            sink.add(tuple(ctx.eval(code) for code in projections))
        return Context({source: sink})
    return join_context


def nested_loop_join(join, a, b, context):
    sink = DataTable(
        list(context.data_tables[a].table) + list(context.data_tables[b].table)
    )
    for _ in context.iter_table(a):
        for ctx in context.iter_table(b):
            sink.add(ctx[a].tuple() + ctx[b].tuple())

    return sink


def sort_merge_join(join, a, b, context):
    on = join["on"]
    on = on.flatten() if isinstance(on, exp.And) else [on]

    a_key = []
    b_key = []

    for condition in on:
        for column in condition.find_all(exp.Column):
            if b == column.text("table"):
                b_key.append(generate(column))
            else:
                a_key.append(generate(exp.column(column.name, a)))

    context.sort(a, lambda c: tuple(c.eval(code) for code in a_key))
    context.sort(b, lambda c: tuple(c.eval(code) for code in b_key))

    a_i = 0
    b_i = 0
    a_n = context.data_tables[a].length
    b_n = context.data_tables[b].length

    sink = DataTable(
        list(context.data_tables[a].table) + list(context.data_tables[b].table)
    )

    def get_key(table, key, i):
        context.set_row(table, i)
        return tuple(context.eval(code) for code in key)

    while a_i < a_n and b_i < b_n:
        key = min(get_key(a, a_key, a_i), get_key(b, b_key, b_i))

        a_group = []

        while a_i < a_n and key == get_key(a, a_key, a_i):
            a_group.append(context[a].tuple())
            a_i += 1

        b_group = []

        while b_i < b_n and key == get_key(b, b_key, b_i):
            b_group.append(context[b].tuple())
            b_i += 1

        for a_row in a_group:
            for b_row in b_group:
                sink.add(a_row + b_row)

    return sink


def aggregate(step, context):
    group = []
    projections = []
    aggregations = []
    columns = []
    operands = []

    for expression in step.group:
        columns.append(expression.alias_or_name)
        projections.append(generate(expression))
    for expression in step.aggregations:
        columns.append(expression.alias_or_name)
        aggregations.append(generate(expression))
    for expression in step.operands:
        columns.append(expression.alias_or_name)
        operands.append(generate(expression))

    table = list(context.data_tables)[0]
    context.sort(table, lambda c: tuple(c.eval(code) for code in projections))

    operand_dt = DataTable(operand.alias_or_name for operand in step.operands)
    for ctx in context.iter_table(table):
        operand_dt.add(tuple(ctx.eval(operand) for operand in operands))

    context = Context({table: DataTable({**context.data_tables[table].table, **operand_dt.table})})
    print(context.data_tables)
    raise

    group = None
    start = 0
    end = 1
    length = context.data_tables[table].length
    sink = DataTable(columns)

    for i in range(length):
        context.set_row(table, i)
        key = tuple(context.eval(code) for code in projections)
        group = key if group is None else group
        end += 1

        if i == length - 1:
            context.set_range(table, start, end - 1)
        elif key != group:
            context.set_range(table, start, end - 2)
        else:
            continue
        aggs = tuple(context.eval(agg) for agg in aggregations)
        sink.add(group + aggs)
        group = key
        start = end - 2

    return Context({step.name: sink})


def sort(step, context):
    keys = tuple(generate(k) for k in step.key)
    table = list(context.data_tables)[0]
    context.sort(table, lambda c: tuple(c.eval(code) for code in keys))

    sink = DataTable(
        expression.alias_or_name for expression in step.projections
    )
    projections = tuple(generate(expression) for expression in step.projections)

    for ctx in context.iter_table(table):
        sink.add(tuple(ctx.eval(code) for code in projections))

        if step.limit and sink.length >= step.limit:
            break
    return Context({step.name: sink})


class DataTable:
    def __init__(self, columns_or_table):
        if isinstance(columns_or_table, dict):
            self.table = columns_or_table
        else:
            self.table = {column: [] for column in columns_or_table}
        self.columns = dict(enumerate(self.table))
        self.width = len(self.columns)
        self.length = 0
        self.reader = ColumnarReader(self.table)

    def __iter__(self):
        return DataTableIter(self)

    def __repr__(self):
        widths = {column: len(column) for column in self.table}
        lines = [" ".join(column for column in self.table)]

        for i, row in enumerate(self):
            if i > 10:
                break

            lines.append(
                " ".join(
                    str(row[column]).rjust(widths[column])[0 : widths[column]]
                    for column in self.table
                )
            )
        return "\n".join(lines)

    def __getitem__(self, column):
        return self.columns[column]

    def add(self, row):
        for i in range(self.width):
            self.table[self.columns[i]].append(row[i])
        self.length += 1

    def pop(self):
        for column in self.table.values():
            column.pop()
        self.length -= 1


class DataTableIter:
    def __init__(self, data_table):
        self.data_table = data_table
        self.index = -1

    def __iter__(self):
        return self

    def __next__(self):
        self.index += 1
        if self.index < self.data_table.length:
            self.data_table.reader.row = self.index
            return self.data_table.reader
        raise StopIteration


class Context:
    """
    Execution context for sql expressions.

    Context is used to hold relevant data tables which can then be queried on with eval.

    References to columns can either be scalar or vectors. When set_row is used, column references
    evaluate to scalars while set_range evaluates to vectors. This allows convenient and efficient
    evaluation of aggregation functions.
    """

    def __init__(self, tables, env=None):
        self.data_tables = {
            name: table
            for name, table in tables.items()
            if isinstance(table, DataTable)
        }
        self.range_readers = {
            name: RangeReader(data_table.table)
            for name, data_table in self.data_tables.items()
        }
        self.row_readers = {
            name: dt_or_columns.reader
            if name in self.data_tables
            else RowReader(dt_or_columns)
            for name, dt_or_columns in tables.items()
        }
        self.env = {**(env or {}), "scope": self.row_readers}

    def eval(self, code):
        # pylint: disable=eval-used
        return eval(code, ENV, self.env)

    def iter_table(self, table):
        for i in range(self.data_tables[table].length):
            self.set_row(table, i)
            yield self

    def sort(self, table, key):
        def _sort(i):
            self.set_row(table, i)
            return key(self)

        data_table = self.data_tables[table]
        index = list(range(data_table.length))
        index.sort(key=_sort)

        for column, rows in data_table.table.items():
            data_table.table[column] = [rows[i] for i in index]

    def set_row(self, table, row):
        self.row_readers[table].row = row
        self.env["scope"] = self.row_readers

    def set_range(self, table, start, end):
        self.range_readers[table].range = range(start, end)
        self.env["scope"] = self.range_readers

    def __getitem__(self, table):
        return self.env["scope"][table]

    def __contains__(self, table):
        return table in self.data_tables


class RangeReader:
    def __init__(self, columns):
        self.columns = columns
        self.range = range(0)

    def __len__(self):
        return len(self.range)

    def __getitem__(self, column):
        return (self.columns[column][i] for i in self.range)


class ColumnarReader:
    def __init__(self, columns):
        self.columns = columns
        self.row = None

    def tuple(self):
        return tuple(self[column] for column in self.columns)

    def __getitem__(self, column):
        return self.columns[column][self.row]


class RowReader:
    def __init__(self, columns):
        self.columns = {column: i for i, column in enumerate(columns)}
        self.row = None

    def tuple(self):
        return tuple(self.row)

    def __getitem__(self, column):
        return self.row[self.columns[column]]


class Python(Dialect):
    # pylint: disable=no-member
    def _cast_py(self, expression):
        to = expression.args["to"].this
        this = self.sql(expression, "this")

        if to == exp.DataType.Type.DATE:
            return f"datetime.date.fromisoformat({this})"
        raise NotImplementedError

    def _column_py(self, expression):
        table = self.sql(expression, "table")
        this = self.sql(expression, "this")
        return f"scope[{table}][{this}]"

    def _interval_py(self, expression):
        this = self.sql(expression, "this")
        unit = expression.text("unit").upper()
        if unit == "DAY":
            return f"datetime.timedelta(days=float({this}))"
        raise NotImplementedError

    def _like_py(self, expression):
        this = self.sql(expression, "this")
        expression = self.sql(expression, "expression")
        return f"""re.match({expression}.replace("_", ".").replace("%", ".*"), {this})"""

    def _ordered_py(self, expression):
        this = self.sql(expression, "this")
        desc = expression.args.get("desc")
        return f"desc({this})" if desc else this

    transforms = {
        exp.Alias: lambda self, e: self.sql(e.this),
        exp.And: lambda self, e: self.binary(e, "and"),
        exp.Cast: _cast_py,
        exp.Column: _column_py,
        exp.EQ: lambda self, e: self.binary(e, "=="),
        exp.Interval: _interval_py,
        exp.Like: _like_py,
        exp.Or: lambda self, e: self.binary(e, "or"),
        exp.Ordered: _ordered_py,
        exp.Star: lambda *_: "1",
    }


PYTHON_GENERATOR = Python().generator(identify=True)
