
"""PostgreSQL connection helpers for PolicyOps."""

import os

import psycopg


def get_connection() -> psycopg.Connection:
    """Connect using local defaults or an explicitly configured database."""

    connection_string = os.getenv(
        "POLICYOPS_DATABASE_URL",
        "dbname=policyops_lab",
    )

    return psycopg.connect(
        connection_string,
        connect_timeout=5,
    )


def main() -> int:
    """Run a read-only check of the database connection and schema."""

    try:
        with get_connection() as connection:
            database_info = connection.execute(
                """
                SELECT
                    current_database(),
                    current_user,
                    current_setting('server_version')
                """
            ).fetchone()

            table_result = connection.execute(
                """
                SELECT COUNT(*)
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_type = 'BASE TABLE'
                """
            ).fetchone()

    except psycopg.OperationalError:
        print(
            "Database connection failed. Check that PostgreSQL "
            "is running and the connection settings are correct."
        )
        return 1

    if database_info is None or table_result is None:
        print("Database verification returned an unexpected result.")
        return 1

    database_name, database_user, server_version = database_info
    table_count = table_result[0]

    print(f"Connected to: {database_name}")
    print(f"Database user: {database_user}")
    print(f"PostgreSQL version: {server_version}")
    print(f"PolicyOps tables found: {table_count}")

    if table_count != 5:
        print("Expected five PolicyOps tables. Check sql/schema.sql.")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
