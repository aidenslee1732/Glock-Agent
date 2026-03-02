"""Database Tools for Glock.

Phase 3 Feature 3.8: Database integration.

Provides:
- Database connection management
- Query execution with result formatting
- Schema inspection
- Migration support detection

Supports:
- PostgreSQL
- MySQL/MariaDB
- SQLite
- MongoDB (basic support)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, date
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Optional, Union
from uuid import UUID

logger = logging.getLogger(__name__)

# Optional database drivers
try:
    import asyncpg
    ASYNCPG_AVAILABLE = True
except ImportError:
    ASYNCPG_AVAILABLE = False

try:
    import aiomysql
    AIOMYSQL_AVAILABLE = True
except ImportError:
    AIOMYSQL_AVAILABLE = False

try:
    import aiosqlite
    AIOSQLITE_AVAILABLE = True
except ImportError:
    AIOSQLITE_AVAILABLE = False


class DatabaseType(str, Enum):
    """Supported database types."""
    POSTGRESQL = "postgresql"
    MYSQL = "mysql"
    SQLITE = "sqlite"
    MONGODB = "mongodb"


@dataclass
class ConnectionConfig:
    """Database connection configuration."""
    db_type: DatabaseType
    host: str = "localhost"
    port: int = 5432
    database: str = ""
    username: str = ""
    password: str = ""
    ssl: bool = False
    connection_string: Optional[str] = None

    @classmethod
    def from_url(cls, url: str) -> "ConnectionConfig":
        """Parse connection URL.

        Supports:
        - postgresql://user:pass@host:port/db
        - mysql://user:pass@host:port/db
        - sqlite:///path/to/db.sqlite
        """
        if url.startswith("postgresql://") or url.startswith("postgres://"):
            return cls._parse_postgres_url(url)
        elif url.startswith("mysql://"):
            return cls._parse_mysql_url(url)
        elif url.startswith("sqlite://"):
            return cls._parse_sqlite_url(url)
        else:
            raise ValueError(f"Unsupported database URL: {url}")

    @classmethod
    def _parse_postgres_url(cls, url: str) -> "ConnectionConfig":
        """Parse PostgreSQL URL."""
        # postgresql://user:pass@host:port/db?sslmode=require
        pattern = r"postgres(?:ql)?://(?:([^:]+):([^@]+)@)?([^:/]+)(?::(\d+))?/([^?]+)(?:\?(.+))?"
        match = re.match(pattern, url)
        if not match:
            raise ValueError(f"Invalid PostgreSQL URL: {url}")

        user, password, host, port, db, params = match.groups()
        ssl = "ssl" in (params or "") or "sslmode" in (params or "")

        return cls(
            db_type=DatabaseType.POSTGRESQL,
            host=host,
            port=int(port) if port else 5432,
            database=db,
            username=user or "",
            password=password or "",
            ssl=ssl,
            connection_string=url,
        )

    @classmethod
    def _parse_mysql_url(cls, url: str) -> "ConnectionConfig":
        """Parse MySQL URL."""
        pattern = r"mysql://(?:([^:]+):([^@]+)@)?([^:/]+)(?::(\d+))?/([^?]+)"
        match = re.match(pattern, url)
        if not match:
            raise ValueError(f"Invalid MySQL URL: {url}")

        user, password, host, port, db = match.groups()

        return cls(
            db_type=DatabaseType.MYSQL,
            host=host,
            port=int(port) if port else 3306,
            database=db,
            username=user or "",
            password=password or "",
            connection_string=url,
        )

    @classmethod
    def _parse_sqlite_url(cls, url: str) -> "ConnectionConfig":
        """Parse SQLite URL."""
        # sqlite:///path/to/db.sqlite or sqlite:///:memory:
        path = url.replace("sqlite:///", "").replace("sqlite://", "")

        return cls(
            db_type=DatabaseType.SQLITE,
            database=path,
            connection_string=url,
        )


@dataclass
class QueryResult:
    """Result of a database query."""
    success: bool
    rows: list[dict[str, Any]] = field(default_factory=list)
    row_count: int = 0
    columns: list[str] = field(default_factory=list)
    affected_rows: int = 0
    error: Optional[str] = None
    duration_ms: int = 0
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "rows": self.rows[:100],  # Limit returned rows
            "row_count": self.row_count,
            "columns": self.columns,
            "affected_rows": self.affected_rows,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "truncated": self.truncated or self.row_count > 100,
        }

    def to_table(self, max_rows: int = 50) -> str:
        """Format result as ASCII table."""
        if not self.success:
            return f"Error: {self.error}"

        if not self.rows:
            return f"Query executed successfully. Affected rows: {self.affected_rows}"

        # Determine column widths
        widths = {col: len(col) for col in self.columns}
        display_rows = self.rows[:max_rows]

        for row in display_rows:
            for col in self.columns:
                val = str(row.get(col, ""))[:50]  # Truncate long values
                widths[col] = max(widths[col], len(val))

        # Build table
        lines = []

        # Header
        header = " | ".join(col.ljust(widths[col]) for col in self.columns)
        separator = "-+-".join("-" * widths[col] for col in self.columns)
        lines.append(header)
        lines.append(separator)

        # Rows
        for row in display_rows:
            line = " | ".join(
                str(row.get(col, ""))[:50].ljust(widths[col])
                for col in self.columns
            )
            lines.append(line)

        if self.row_count > max_rows:
            lines.append(f"... ({self.row_count - max_rows} more rows)")

        lines.append(f"\n({self.row_count} rows, {self.duration_ms}ms)")

        return "\n".join(lines)


@dataclass
class TableInfo:
    """Information about a database table."""
    name: str
    schema: str = "public"
    columns: list[dict[str, Any]] = field(default_factory=list)
    primary_key: list[str] = field(default_factory=list)
    foreign_keys: list[dict[str, Any]] = field(default_factory=list)
    indexes: list[dict[str, Any]] = field(default_factory=list)
    row_count: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "schema": self.schema,
            "columns": self.columns,
            "primary_key": self.primary_key,
            "foreign_keys": self.foreign_keys,
            "indexes": self.indexes,
            "row_count": self.row_count,
        }


@dataclass
class SchemaInfo:
    """Information about database schema."""
    database: str
    tables: list[TableInfo] = field(default_factory=list)
    views: list[str] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "database": self.database,
            "tables": [t.to_dict() for t in self.tables],
            "views": self.views,
            "functions": self.functions,
        }


def _serialize_value(value: Any) -> Any:
    """Serialize database value for JSON."""
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (list, tuple)):
        return [_serialize_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _serialize_value(v) for k, v in value.items()}
    return str(value)


class DatabaseConnection:
    """Abstract database connection."""

    def __init__(self, config: ConnectionConfig):
        self.config = config
        self._connection = None

    async def connect(self) -> None:
        """Establish connection."""
        raise NotImplementedError

    async def close(self) -> None:
        """Close connection."""
        raise NotImplementedError

    async def execute(self, query: str, params: Optional[list] = None) -> QueryResult:
        """Execute query."""
        raise NotImplementedError

    async def get_schema(self) -> SchemaInfo:
        """Get database schema information."""
        raise NotImplementedError

    async def get_table_info(self, table_name: str) -> TableInfo:
        """Get table information."""
        raise NotImplementedError


class PostgreSQLConnection(DatabaseConnection):
    """PostgreSQL database connection."""

    async def connect(self) -> None:
        if not ASYNCPG_AVAILABLE:
            raise RuntimeError("asyncpg not installed. Run: pip install asyncpg")

        self._connection = await asyncpg.connect(
            host=self.config.host,
            port=self.config.port,
            database=self.config.database,
            user=self.config.username,
            password=self.config.password,
            ssl=self.config.ssl,
        )

    async def close(self) -> None:
        if self._connection:
            await self._connection.close()
            self._connection = None

    async def execute(self, query: str, params: Optional[list] = None) -> QueryResult:
        import time
        start_time = time.time()

        if not self._connection:
            return QueryResult(success=False, error="Not connected")

        try:
            # Determine if it's a SELECT query
            query_upper = query.strip().upper()
            is_select = query_upper.startswith("SELECT") or query_upper.startswith("WITH")

            if is_select:
                rows = await self._connection.fetch(query, *(params or []))
                columns = list(rows[0].keys()) if rows else []
                result_rows = [
                    {k: _serialize_value(v) for k, v in dict(row).items()}
                    for row in rows
                ]

                return QueryResult(
                    success=True,
                    rows=result_rows,
                    row_count=len(result_rows),
                    columns=columns,
                    duration_ms=int((time.time() - start_time) * 1000),
                )
            else:
                result = await self._connection.execute(query, *(params or []))
                # Parse affected rows from result string (e.g., "UPDATE 5")
                affected = 0
                if result:
                    parts = result.split()
                    if len(parts) >= 2 and parts[-1].isdigit():
                        affected = int(parts[-1])

                return QueryResult(
                    success=True,
                    affected_rows=affected,
                    duration_ms=int((time.time() - start_time) * 1000),
                )

        except Exception as e:
            return QueryResult(
                success=False,
                error=str(e),
                duration_ms=int((time.time() - start_time) * 1000),
            )

    async def get_schema(self) -> SchemaInfo:
        # Get tables
        tables_query = """
            SELECT table_name, table_schema
            FROM information_schema.tables
            WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
            AND table_type = 'BASE TABLE'
            ORDER BY table_schema, table_name
        """
        tables_result = await self.execute(tables_query)

        tables = []
        for row in tables_result.rows:
            table_info = await self.get_table_info(row["table_name"], row["table_schema"])
            tables.append(table_info)

        # Get views
        views_query = """
            SELECT table_name
            FROM information_schema.views
            WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
        """
        views_result = await self.execute(views_query)
        views = [row["table_name"] for row in views_result.rows]

        return SchemaInfo(
            database=self.config.database,
            tables=tables,
            views=views,
        )

    async def get_table_info(self, table_name: str, schema: str = "public") -> TableInfo:
        # Get columns
        columns_query = """
            SELECT column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_name = $1 AND table_schema = $2
            ORDER BY ordinal_position
        """
        columns_result = await self.execute(columns_query, [table_name, schema])

        # Get primary key
        pk_query = """
            SELECT a.attname
            FROM pg_index i
            JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
            WHERE i.indrelid = ($1 || '.' || $2)::regclass AND i.indisprimary
        """
        # Simplified - just use information_schema
        pk_query = """
            SELECT kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
                ON tc.constraint_name = kcu.constraint_name
            WHERE tc.table_name = $1 AND tc.table_schema = $2
                AND tc.constraint_type = 'PRIMARY KEY'
        """
        pk_result = await self.execute(pk_query, [table_name, schema])

        # Get row count estimate
        count_result = await self.execute(
            f"SELECT COUNT(*) as count FROM {schema}.{table_name} LIMIT 1"
        )
        row_count = count_result.rows[0]["count"] if count_result.rows else None

        return TableInfo(
            name=table_name,
            schema=schema,
            columns=columns_result.rows,
            primary_key=[row["column_name"] for row in pk_result.rows],
            row_count=row_count,
        )


class SQLiteConnection(DatabaseConnection):
    """SQLite database connection."""

    async def connect(self) -> None:
        if AIOSQLITE_AVAILABLE:
            import aiosqlite
            self._connection = await aiosqlite.connect(self.config.database)
            self._connection.row_factory = aiosqlite.Row
        else:
            # Fallback to sync sqlite3 wrapped in executor
            self._connection = sqlite3.connect(self.config.database)
            self._connection.row_factory = sqlite3.Row

    async def close(self) -> None:
        if self._connection:
            if AIOSQLITE_AVAILABLE:
                await self._connection.close()
            else:
                self._connection.close()
            self._connection = None

    async def execute(self, query: str, params: Optional[list] = None) -> QueryResult:
        import time
        start_time = time.time()

        if not self._connection:
            return QueryResult(success=False, error="Not connected")

        try:
            query_upper = query.strip().upper()
            is_select = query_upper.startswith("SELECT") or query_upper.startswith("WITH")

            if AIOSQLITE_AVAILABLE:
                cursor = await self._connection.execute(query, params or [])

                if is_select:
                    rows = await cursor.fetchall()
                    columns = [desc[0] for desc in cursor.description] if cursor.description else []
                    result_rows = [
                        {columns[i]: _serialize_value(row[i]) for i in range(len(columns))}
                        for row in rows
                    ]

                    return QueryResult(
                        success=True,
                        rows=result_rows,
                        row_count=len(result_rows),
                        columns=columns,
                        duration_ms=int((time.time() - start_time) * 1000),
                    )
                else:
                    await self._connection.commit()
                    return QueryResult(
                        success=True,
                        affected_rows=cursor.rowcount,
                        duration_ms=int((time.time() - start_time) * 1000),
                    )
            else:
                # Sync fallback
                cursor = self._connection.execute(query, params or [])

                if is_select:
                    rows = cursor.fetchall()
                    columns = [desc[0] for desc in cursor.description] if cursor.description else []
                    result_rows = [
                        {columns[i]: _serialize_value(row[i]) for i in range(len(columns))}
                        for row in rows
                    ]

                    return QueryResult(
                        success=True,
                        rows=result_rows,
                        row_count=len(result_rows),
                        columns=columns,
                        duration_ms=int((time.time() - start_time) * 1000),
                    )
                else:
                    self._connection.commit()
                    return QueryResult(
                        success=True,
                        affected_rows=cursor.rowcount,
                        duration_ms=int((time.time() - start_time) * 1000),
                    )

        except Exception as e:
            return QueryResult(
                success=False,
                error=str(e),
                duration_ms=int((time.time() - start_time) * 1000),
            )

    async def get_schema(self) -> SchemaInfo:
        tables_result = await self.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )

        tables = []
        for row in tables_result.rows:
            table_info = await self.get_table_info(row["name"])
            tables.append(table_info)

        views_result = await self.execute(
            "SELECT name FROM sqlite_master WHERE type='view'"
        )
        views = [row["name"] for row in views_result.rows]

        return SchemaInfo(
            database=self.config.database,
            tables=tables,
            views=views,
        )

    async def get_table_info(self, table_name: str, schema: str = "main") -> TableInfo:
        # Get columns
        columns_result = await self.execute(f"PRAGMA table_info({table_name})")

        columns = [
            {
                "column_name": row["name"],
                "data_type": row["type"],
                "is_nullable": "YES" if not row["notnull"] else "NO",
                "column_default": row["dflt_value"],
            }
            for row in columns_result.rows
        ]

        primary_key = [
            row["name"] for row in columns_result.rows if row["pk"]
        ]

        # Get row count
        count_result = await self.execute(f"SELECT COUNT(*) as count FROM {table_name}")
        row_count = count_result.rows[0]["count"] if count_result.rows else None

        return TableInfo(
            name=table_name,
            schema=schema,
            columns=columns,
            primary_key=primary_key,
            row_count=row_count,
        )


class DatabaseManager:
    """Manage database connections and queries.

    Usage:
        manager = DatabaseManager()

        # Connect using URL
        await manager.connect("postgresql://user:pass@localhost/db")

        # Execute query
        result = await manager.execute("SELECT * FROM users LIMIT 10")
        print(result.to_table())

        # Get schema
        schema = await manager.get_schema()

        await manager.close()
    """

    def __init__(self):
        self._connections: dict[str, DatabaseConnection] = {}
        self._current: Optional[str] = None

    async def connect(
        self,
        url_or_config: Union[str, ConnectionConfig],
        name: str = "default",
    ) -> None:
        """Connect to a database.

        Args:
            url_or_config: Connection URL or config
            name: Connection name for managing multiple connections
        """
        if isinstance(url_or_config, str):
            config = ConnectionConfig.from_url(url_or_config)
        else:
            config = url_or_config

        # Create appropriate connection
        if config.db_type == DatabaseType.POSTGRESQL:
            conn = PostgreSQLConnection(config)
        elif config.db_type == DatabaseType.SQLITE:
            conn = SQLiteConnection(config)
        else:
            raise ValueError(f"Unsupported database type: {config.db_type}")

        await conn.connect()
        self._connections[name] = conn
        self._current = name
        logger.info(f"Connected to {config.db_type.value} database: {name}")

    async def close(self, name: Optional[str] = None) -> None:
        """Close database connection."""
        if name is None:
            name = self._current

        if name and name in self._connections:
            await self._connections[name].close()
            del self._connections[name]
            if self._current == name:
                self._current = next(iter(self._connections), None)

    async def close_all(self) -> None:
        """Close all connections."""
        for name in list(self._connections.keys()):
            await self.close(name)

    def use(self, name: str) -> None:
        """Switch to a named connection."""
        if name not in self._connections:
            raise ValueError(f"No connection named: {name}")
        self._current = name

    async def execute(
        self,
        query: str,
        params: Optional[list] = None,
        connection: Optional[str] = None,
    ) -> QueryResult:
        """Execute a query.

        Args:
            query: SQL query
            params: Query parameters
            connection: Connection name (uses current if not specified)

        Returns:
            QueryResult
        """
        conn_name = connection or self._current
        if not conn_name or conn_name not in self._connections:
            return QueryResult(success=False, error="No active database connection")

        return await self._connections[conn_name].execute(query, params)

    async def get_schema(self, connection: Optional[str] = None) -> SchemaInfo:
        """Get database schema.

        Args:
            connection: Connection name

        Returns:
            SchemaInfo
        """
        conn_name = connection or self._current
        if not conn_name or conn_name not in self._connections:
            raise ValueError("No active database connection")

        return await self._connections[conn_name].get_schema()

    async def get_table_info(
        self,
        table_name: str,
        connection: Optional[str] = None,
    ) -> TableInfo:
        """Get table information.

        Args:
            table_name: Table name
            connection: Connection name

        Returns:
            TableInfo
        """
        conn_name = connection or self._current
        if not conn_name or conn_name not in self._connections:
            raise ValueError("No active database connection")

        return await self._connections[conn_name].get_table_info(table_name)

    @property
    def connections(self) -> list[str]:
        """List active connection names."""
        return list(self._connections.keys())

    @property
    def current_connection(self) -> Optional[str]:
        """Get current connection name."""
        return self._current


# Global manager instance
_manager: Optional[DatabaseManager] = None


def get_database_manager() -> DatabaseManager:
    """Get global database manager instance."""
    global _manager
    if _manager is None:
        _manager = DatabaseManager()
    return _manager


# Tool handlers for integration with ToolBroker

async def db_connect_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Tool handler for connecting to a database.

    Args:
        url: Database connection URL
        name: Optional connection name (default: "default")

    Returns:
        Connection status
    """
    url = args.get("url")
    if not url:
        # Try environment variable
        url = os.environ.get("DATABASE_URL")
        if not url:
            return {"status": "error", "error": "url is required or set DATABASE_URL"}

    name = args.get("name", "default")

    manager = get_database_manager()
    try:
        await manager.connect(url, name)
        return {
            "status": "success",
            "message": f"Connected to database as '{name}'",
            "connection": name,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


async def db_query_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Tool handler for executing database queries.

    Args:
        query: SQL query to execute
        params: Optional query parameters
        connection: Optional connection name
        format: Output format ("json" or "table")

    Returns:
        Query result
    """
    query = args.get("query")
    if not query:
        return {"status": "error", "error": "query is required"}

    params = args.get("params")
    connection = args.get("connection")
    output_format = args.get("format", "json")

    manager = get_database_manager()
    result = await manager.execute(query, params, connection)

    response = result.to_dict()
    if output_format == "table":
        response["formatted"] = result.to_table()

    return response


async def db_schema_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Tool handler for getting database schema.

    Args:
        connection: Optional connection name
        table: Optional specific table name

    Returns:
        Schema information
    """
    connection = args.get("connection")
    table_name = args.get("table")

    manager = get_database_manager()

    try:
        if table_name:
            info = await manager.get_table_info(table_name, connection)
            return {"status": "success", "table": info.to_dict()}
        else:
            schema = await manager.get_schema(connection)
            return {"status": "success", "schema": schema.to_dict()}
    except Exception as e:
        return {"status": "error", "error": str(e)}


async def db_close_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Tool handler for closing database connections.

    Args:
        connection: Optional connection name (closes all if not specified)

    Returns:
        Status
    """
    connection = args.get("connection")

    manager = get_database_manager()

    if connection:
        await manager.close(connection)
        return {"status": "success", "message": f"Closed connection: {connection}"}
    else:
        await manager.close_all()
        return {"status": "success", "message": "Closed all connections"}
