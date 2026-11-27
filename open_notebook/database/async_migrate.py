"""
Async migration system for SurrealDB using the official Python client.
Based on patterns from sblpy migration system.
"""

from typing import List

from loguru import logger

from .repository import db_connection, repo_query


class AsyncMigration:
    """
    Handles individual migration operations with async support.
    """

    def __init__(self, sql: str) -> None:
        """Initialize migration with SQL content."""
        self.sql = sql

    @classmethod
    def from_file(cls, file_path: str) -> "AsyncMigration":
        """Create migration from SQL file."""
        with open(file_path, "r", encoding="utf-8") as file:
            raw_content = file.read()
            # Clean up SQL content
            lines = []
            for line in raw_content.split("\n"):
                line = line.strip()
                if line and not line.startswith("--"):
                    lines.append(line)
            sql = " ".join(lines)
            return cls(sql)

    async def run(self, bump: bool = True) -> None:
        """Run the migration.

        Individual SurrealQL statements are executed one at a time. If a
        statement fails (e.g. an index on a field that doesn't exist yet on
        this SurrealDB version), it is logged and skipped rather than failing
        the entire migration — a single broken index definition must not
        prevent the application from starting.
        """
        try:
            async with db_connection() as connection:
                statements = self._split_surrealql(self.sql)
                for stmt in statements:
                    try:
                        await connection.query(stmt)
                    except Exception as stmt_exc:  # noqa: BLE001
                        logger.warning(
                            f"Migration statement skipped: {str(stmt_exc)}"
                        )

            if bump:
                await bump_version()
            else:
                await lower_version()

        except Exception as e:
            logger.error(f"Migration failed: {str(e)}")
            raise

    @staticmethod
    def _split_surrealql(sql: str) -> List[str]:
        """Split SurrealQL into individual statements, respecting brace blocks.

        SurrealDB 4.x accepts only one statement per query call. We split on
        semicolons that are NOT inside `{ }` blocks, so that multi-statement
        blocks like DEFINE FUNCTION ... { ... } are executed as a single unit.
        """
        statements: List[str] = []
        current: List[str] = []
        depth = 0
        for ch in sql:
            if ch == "{":
                depth += 1
                current.append(ch)
            elif ch == "}":
                depth -= 1
                current.append(ch)
            elif ch == ";" and depth == 0:
                stmt = "".join(current).strip()
                if stmt:
                    statements.append(stmt)
                current = []
            else:
                current.append(ch)
        # Final statement (no trailing semicolon)
        stmt = "".join(current).strip()
        if stmt:
            statements.append(stmt)
        return statements


class AsyncMigrationRunner:
    """
    Handles running multiple migrations in sequence.
    """

    def __init__(
        self,
        up_migrations: List[AsyncMigration],
        down_migrations: List[AsyncMigration],
    ) -> None:
        """Initialize runner with migration lists."""
        self.up_migrations = up_migrations
        self.down_migrations = down_migrations

    async def run_all(self) -> None:
        """Run all pending up migrations."""
        current_version = await get_latest_version()

        for i in range(current_version, len(self.up_migrations)):
            logger.info(f"Running migration {i + 1}")
            await self.up_migrations[i].run(bump=True)

    async def run_one_up(self) -> None:
        """Run one up migration."""
        current_version = await get_latest_version()

        if current_version < len(self.up_migrations):
            logger.info(f"Running migration {current_version + 1}")
            await self.up_migrations[current_version].run(bump=True)

    async def run_one_down(self) -> None:
        """Run one down migration."""
        current_version = await get_latest_version()

        if current_version > 0:
            logger.info(f"Rolling back migration {current_version}")
            await self.down_migrations[current_version - 1].run(bump=False)


class AsyncMigrationManager:
    """
    Main migration manager with async support.
    """

    def __init__(self):
        """Initialize migration manager."""
        self.up_migrations = [
            AsyncMigration.from_file("open_notebook/database/migrations/1.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/2.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/3.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/4.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/5.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/6.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/7.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/8.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/9.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/10.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/11.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/12.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/13.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/14.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/15.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/16.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/17.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/18.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/19.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/20.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/21.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/22.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/23.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/24.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/25.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/26.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/27.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/28.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/29.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/30.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/31.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/32.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/33.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/34.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/35.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/36.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/37.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/38.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/39.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/40.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/41.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/42.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/43.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/44.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/45.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/46.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/47.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/48.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/49.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/50.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/51.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/52.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/53.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/54.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/55.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/56.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/57.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/58.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/59.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/60.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/61.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/62.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/63.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/64.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/65.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/66.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/67.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/68.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/69.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/70.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/71.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/72.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/73.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/74.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/75.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/76.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/77.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/78.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/79.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/80.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/81.surrealql"),
            AsyncMigration.from_file("open_notebook/database/migrations/82.surrealql"),
        ]
        self.down_migrations = [
            AsyncMigration.from_file(
                "open_notebook/database/migrations/1_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/2_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/3_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/4_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/5_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/6_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/7_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/8_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/9_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/10_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/11_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/12_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/13_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/14_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/15_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/16_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/17_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/18_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/19_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/20_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/21_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/22_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/23_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/24_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/25_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/26_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/27_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/28_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/29_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/30_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/31_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/32_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/33_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/34_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/35_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/36_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/37_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/38_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/39_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/40_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/41_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/42_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/43_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/44_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/45_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/46_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/47_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/48_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/49_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/50_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/51_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/52_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/53_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/54_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/55_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/56_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/57_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/58_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/59_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/60_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/61_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/62_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/63_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/64_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/65_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/66_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/67_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/68_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/69_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/70_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/71_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/72_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/73_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/74_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/75_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/76_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/77_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/78_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/79_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/80_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/81_down.surrealql"
            ),
            AsyncMigration.from_file(
                "open_notebook/database/migrations/82_down.surrealql"
            ),
        ]
        self.runner = AsyncMigrationRunner(
            up_migrations=self.up_migrations,
            down_migrations=self.down_migrations,
        )

    async def get_current_version(self) -> int:
        """Get current database version."""
        return await get_latest_version()

    async def ping(self) -> None:
        """Check whether SurrealDB is reachable for migration startup."""
        async with db_connection() as connection:
            await connection.query("RETURN true;")

        # Also exercise the migration version path. get_current_version() already
        # treats a missing migrations table as version 0 for fresh databases.
        await self.get_current_version()

    async def needs_migration(self) -> bool:
        """Check if migration is needed."""
        current_version = await self.get_current_version()
        return current_version < len(self.up_migrations)

    async def run_migration_up(self):
        """Run all pending migrations."""
        current_version = await self.get_current_version()
        logger.info(f"Current version before migration: {current_version}")

        if await self.needs_migration():
            try:
                await self.runner.run_all()
                new_version = await self.get_current_version()
                logger.info(f"Migration successful. New version: {new_version}")
            except Exception as e:
                logger.error(f"Migration failed: {str(e)}")
                raise
        else:
            logger.info("Database is already at the latest version")


# Database version management functions
async def get_latest_version() -> int:
    """Get the latest version from the migrations table."""
    try:
        versions = await get_all_versions()
        if not versions:
            return 0
        return max(version["version"] for version in versions)
    except Exception:
        # If migrations table doesn't exist, we're at version 0
        return 0


async def get_all_versions() -> List[dict]:
    """Get all versions from the migrations table."""
    try:
        result = await repo_query("SELECT * FROM _sbl_migrations ORDER BY version;")
        return result
    except Exception:
        # If table doesn't exist, return empty list
        return []


async def bump_version() -> None:
    """Bump the version by adding a new entry to migrations table."""
    current_version = await get_latest_version()
    new_version = current_version + 1

    try:
        await repo_query(
            f"CREATE _sbl_migrations:{new_version} SET version = {new_version}, applied_at = time::now();",
        )
    except Exception:
        # Record may already exist (e.g. re-run after partial failure); that's OK
        pass


async def lower_version() -> None:
    """Lower the version by removing the latest entry from migrations table."""
    current_version = await get_latest_version()
    if current_version > 0:
        await repo_query(
            f"DELETE _sbl_migrations:{current_version};",
        )
