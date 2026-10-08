"""Regression checks for refusal before privilege or ownership changes."""
from __future__ import annotations

import json
from contextlib import nullcontext
from copy import deepcopy

import psycopg
import pytest
from sqlalchemy.engine import URL

from knowledge_layer import registry_bootstrap


def configure(monkeypatch, *, extra=None):
    query={"host":"/cloudsql/fixture-project:us-central1:fixture-registry", **(extra or {})}
    values={}
    for key,user in (("KNOWLEDGE_BOOTSTRAP_DATABASE_URL","postgres"),("DATABASE_URL","knowledge_layer")):
        values[key]=URL.create("postgresql+psycopg",username=user,password="test",database="knowledge_layer",query=query).render_as_string(hide_password=False)
    monkeypatch.setattr(registry_bootstrap,"operator_secret",values.get)


@pytest.mark.parametrize("extra",[{"dbname":"empty_scratch"},{"service":"alternate"},{"options":"-c search_path=customer"},{"port":"6432"}])
def test_connection_overrides_refuse_before_connection(monkeypatch, extra):
    configure(monkeypatch,extra=extra)
    def unexpected(*args,**kwargs):
        pytest.fail("An override reached PostgreSQL")
    monkeypatch.setattr(registry_bootstrap.psycopg,"connect",unexpected)
    with pytest.raises(ValueError,match="inputs"):
        registry_bootstrap.bootstrap()


@pytest.mark.parametrize("answers",[
    [("empty_scratch",)], [("knowledge_layer",),(1,)],
    [("knowledge_layer",),None,(1,)],
])
def test_existing_role_wrong_database_or_custom_objects_prevent_all_mutations(monkeypatch, answers):
    configure(monkeypatch)
    queries=[]
    remaining=iter(answers)
    class Cursor:
        def execute(self,query,*args):
            assert isinstance(query,str), "A privilege-changing statement reached PostgreSQL"
            queries.append(query)
        def fetchone(self): return next(remaining)
    class Connection:
        def cursor(self): return nullcontext(Cursor())
    monkeypatch.setattr(registry_bootstrap.psycopg,"connect",lambda *a,**k:nullcontext(Connection()))
    with pytest.raises(ValueError): registry_bootstrap.bootstrap()
    if len(queries)==3:
        guard=queries[-1]
        assert "left(nspname, 3) <> 'pg_'" in guard
        assert "LIKE 'pg_%'" not in guard
        for catalog in ("pg_largeobject_metadata","pg_event_trigger","pg_foreign_server",
                        "pg_collation","pg_operator","pg_opclass","pg_opfamily",
                        "pg_publication","pg_subscription","pg_default_acl",
                        "pg_ts_config","pg_ts_dict","pg_ts_parser","pg_ts_template"):
            assert catalog in guard


class Pg18AuthorityModel:
    """Model the documented non-superuser PG18 ownership checks, not a server."""

    def __init__(self, *, authority=(True,), fail_stage=None):
        self.authority = authority
        self.fail_stage = fail_stage
        self.queries = []
        self.mutations = []
        self.committed = False
        self.rolled_back = False
        self.state = {
            "role_exists": False, "automatic_admin": False, "self_set": False,
            "self_inherit": False, "temporary_create": False,
            "database_owner": "postgres", "schema_owner": "pg_database_owner",
            "public_schema_create": True, "public_database_access": True,
            "application_access": False,
        }

    def __enter__(self):
        self.before = deepcopy(self.state)
        return self

    def __exit__(self, exc_type, exc, traceback):
        if exc_type is None:
            self.committed = True
        else:
            self.state = self.before
            self.rolled_back = True
        return False

    def cursor(self):
        return nullcontext(self)

    def _require(self, condition):
        if not condition:
            raise psycopg.errors.InsufficientPrivilege("private PostgreSQL diagnostic password=test")

    def execute(self, query, parameters=None):
        query = query if isinstance(query, str) else query.as_string()
        query = " ".join(query.split())
        self.queries.append((query, parameters))
        read_stages = {
            "SELECT current_database()": "database_identity",
            "SELECT 1 FROM pg_roles": "role_absence",
            "SELECT 1 FROM pg_namespace": "empty_registry",
            "SELECT current_user =": "operator_authority",
        }
        if any(query.startswith(prefix) and self.fail_stage == stage for prefix, stage in read_stages.items()):
            raise psycopg.errors.InsufficientPrivilege("private PostgreSQL diagnostic password=test")
        if query == "SELECT current_database()":
            self.result = ("knowledge_layer",)
            return
        if query.startswith("SELECT 1 FROM pg_roles"):
            self.result = None
            return
        if query.startswith("SELECT 1 FROM pg_namespace"):
            self.result = None
            return
        if query.startswith("SELECT current_user ="):
            assert parameters == ("postgres", "postgres")
            assert "r.rolcreaterole AND r.rolcreatedb" in query
            assert "pg_has_role(current_user, d.datdba, 'USAGE')" in query
            assert "pg_has_role(current_user, n.nspowner, 'USAGE')" in query
            self.result = self.authority
            return
        s = self.state
        schema_authority = s["schema_owner"] == "postgres" or (
            s["schema_owner"] == "pg_database_owner" and s["database_owner"] == "postgres"
        )
        if query.startswith("CREATE ROLE"):
            stage = "create_application_role"
            self._require(not s["role_exists"])
            for attribute in ("NOSUPERUSER", "NOCREATEDB", "NOCREATEROLE", "NOREPLICATION", "NOBYPASSRLS"):
                assert attribute in query
            s["role_exists"] = True
            # Non-superuser CREATEROLE gives ADMIN only, granted by the bootstrap superuser.
            s["automatic_admin"] = True
        elif query.startswith('GRANT "knowledge_layer" TO "postgres"'):
            stage = "temporary_owner_membership"
            self._require(s["automatic_admin"])
            assert query == ('GRANT "knowledge_layer" TO "postgres" WITH ADMIN FALSE, '
                             'INHERIT FALSE, SET TRUE GRANTED BY "postgres"')
            s["self_set"] = True
        elif query.startswith("GRANT CREATE ON DATABASE"):
            stage = "temporary_schema_authority"
            self._require(s["database_owner"] == "postgres")
            s["temporary_create"] = True
        elif query == "REVOKE CREATE ON SCHEMA public FROM PUBLIC":
            stage = "restrict_public_schema"
            self._require(schema_authority)
            s["public_schema_create"] = False
        elif query.startswith("ALTER SCHEMA public OWNER TO"):
            stage = "transfer_schema"
            self._require(schema_authority and s["self_set"] and s["temporary_create"])
            s["schema_owner"] = "knowledge_layer"
        elif query.startswith("REVOKE CREATE ON DATABASE"):
            stage = "remove_temporary_schema_authority"
            self._require(s["database_owner"] == "postgres")
            s["temporary_create"] = False
        elif query.startswith("REVOKE ALL ON DATABASE"):
            stage = "restrict_public_database"
            self._require(s["database_owner"] == "postgres")
            s["public_database_access"] = False
        elif query.startswith("GRANT CONNECT, TEMPORARY ON DATABASE"):
            stage = "application_database_access"
            self._require(s["database_owner"] == "postgres")
            s["application_access"] = True
        elif query.startswith("ALTER DATABASE"):
            stage = "transfer_database"
            self._require(s["database_owner"] == "postgres" and s["self_set"])
            s["database_owner"] = "knowledge_layer"
        elif query.startswith('REVOKE "knowledge_layer" FROM "postgres"'):
            stage = "remove_temporary_owner_membership"
            self._require(s["automatic_admin"])
            assert query == 'REVOKE "knowledge_layer" FROM "postgres" GRANTED BY "postgres"'
            s["self_set"] = False
        else:
            pytest.fail("Unexpected bootstrap statement")
        self.mutations.append((stage, query))
        if stage == self.fail_stage:
            raise psycopg.errors.InsufficientPrivilege("private PostgreSQL diagnostic password=test")

    def fetchone(self):
        return self.result


def install_model(monkeypatch, **kwargs):
    configure(monkeypatch)
    model = Pg18AuthorityModel(**kwargs)
    monkeypatch.setattr(registry_bootstrap.psycopg, "connect", lambda *a, **k: model)
    return model


def test_pg18_non_superuser_role_transfer_and_cleanup(monkeypatch):
    model = install_model(monkeypatch)
    assert registry_bootstrap.bootstrap() == {
        "database": "knowledge_layer", "role": "knowledge_layer",
        "superuser": False, "create_database": False, "create_role": False,
    }
    assert model.committed and not model.rolled_back
    assert model.state == {
        "role_exists": True, "automatic_admin": True, "self_set": False,
        "self_inherit": False, "temporary_create": False,
        "database_owner": "knowledge_layer", "schema_owner": "knowledge_layer",
        "public_schema_create": False, "public_database_access": False,
        "application_access": True,
    }
    stages = [stage for stage, _ in model.mutations]
    assert stages.index("transfer_schema") < stages.index("transfer_database")
    assert stages.index("remove_temporary_schema_authority") < stages.index("transfer_database")
    assert stages[-1] == "remove_temporary_owner_membership"
    assert not any("cloudsqlsuperuser" in query or 'GRANT "postgres"' in query for _, query in model.mutations)


def test_pg18_old_database_first_order_lacks_set_and_loses_schema_authority(monkeypatch):
    successful = install_model(monkeypatch)
    registry_bootstrap.bootstrap()
    statements = dict(successful.mutations)
    no_set = Pg18AuthorityModel()
    no_set.execute(statements["create_application_role"])
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        no_set.execute(statements["transfer_database"])
    database_first = Pg18AuthorityModel()
    for stage in ("create_application_role", "temporary_owner_membership", "temporary_schema_authority",
                  "transfer_database"):
        database_first.execute(statements[stage])
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        database_first.execute(statements["restrict_public_schema"])


@pytest.mark.parametrize("authority", [(False,), None])
def test_operator_without_identity_and_ownership_authority_refuses_before_mutations(monkeypatch, authority):
    model = install_model(monkeypatch, authority=authority)
    with pytest.raises(ValueError, match="ownership authority"):
        registry_bootstrap.bootstrap()
    assert not model.mutations and model.rolled_back and not model.committed


@pytest.mark.parametrize("stage", ["database_identity", "role_absence", "empty_registry", "operator_authority"])
def test_catalog_or_operator_permission_denial_remains_fail_closed(monkeypatch, capsys, stage):
    model = install_model(monkeypatch, fail_stage=stage)
    assert registry_bootstrap.main() == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "refused", "category": "database", "stage": stage, "sqlstate": "42501",
    }
    assert model.rolled_back and not model.committed and not model.mutations


@pytest.mark.parametrize("stage", [
    "create_application_role", "temporary_owner_membership", "temporary_schema_authority",
    "restrict_public_schema", "transfer_schema", "remove_temporary_schema_authority",
    "restrict_public_database", "application_database_access", "transfer_database",
    "remove_temporary_owner_membership",
])
def test_all_mutations_and_temporary_grants_roll_back_on_refusal(monkeypatch, capsys, stage):
    model = install_model(monkeypatch, fail_stage=stage)
    initial = deepcopy(model.state)
    assert registry_bootstrap.main() == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "refused", "category": "database", "stage": stage, "sqlstate": "42501",
    }
    assert model.rolled_back and not model.committed and model.state == initial
    assert model.mutations[-1][0] == stage


@pytest.mark.parametrize("sqlstate", [None, "42501 password=test", "x\ncredential"])
def test_database_error_sqlstate_sanitized(monkeypatch, capsys, sqlstate):
    def fail():
        raise registry_bootstrap.BootstrapDatabaseError("empty_registry", sqlstate)
    monkeypatch.setattr(registry_bootstrap, "bootstrap", fail)
    assert registry_bootstrap.main() == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "refused", "category": "database", "stage": "empty_registry", "sqlstate": "unavailable",
    }


def test_connection_failure_does_not_print_server_credentials(monkeypatch, capsys):
    configure(monkeypatch)
    def fail(*args, **kwargs):
        raise psycopg.OperationalError("postgresql://postgres:private@private/knowledge_layer")
    monkeypatch.setattr(registry_bootstrap.psycopg, "connect", fail)
    assert registry_bootstrap.main() == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "refused", "category": "database", "stage": "connection_or_commit", "sqlstate": "unavailable",
    }
