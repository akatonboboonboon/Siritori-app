from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parent.parent


class DeploymentConfigurationTests(unittest.TestCase):
    def test_render_python_version_matches_ci(self) -> None:
        python_version = (ROOT / ".python-version").read_text(encoding="utf-8")

        self.assertEqual("3.13", python_version.strip())

    def test_render_runs_migrations_and_checks_database_readiness(self) -> None:
        blueprint = (ROOT / "render.yaml").read_text(encoding="utf-8")

        self.assertIn("startCommand: python -m scripts.start", blueprint)
        self.assertIn("healthCheckPath: /readyz", blueprint)
        self.assertIn("key: DATABASE_URL", blueprint)
        self.assertIn("key: DIRECT_DATABASE_URL", blueprint)
        self.assertGreaterEqual(blueprint.count("sync: false"), 2)
        self.assertGreaterEqual(blueprint.count("generateValue: true"), 2)

    def test_required_production_dependencies_are_pinned(self) -> None:
        requirements = set(
            (ROOT / "requirements.txt")
            .read_text(encoding="utf-8")
            .splitlines()
        )

        self.assertTrue(
            {
                "nicegui==3.14.0",
                "sudachipy==0.6.11",
                "sudachidict-full==20260723",
                "SQLAlchemy==2.0.51",
                "alembic==1.18.5",
                "argon2-cffi==25.1.0",
                "psycopg[binary]==3.3.4",
            }.issubset(requirements)
        )
        self.assertNotIn("sudachidict-core==20260428", requirements)

    def test_local_secrets_and_databases_are_ignored(self) -> None:
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(".env", ignore)
        self.assertIn("!.env.example", ignore)
        self.assertIn("*.db", ignore)


if __name__ == "__main__":
    unittest.main()
