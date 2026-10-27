from contextlib import closing

from invoice_agent import db


def inventory(path):
    with closing(db.connect(path)) as conn:
        return {r["item"]: (r["stock"], r["unit_price"]) for r in conn.execute("SELECT * FROM inventory")}


def test_first_run_seeds_inventory(tmp_path):
    path = tmp_path / "inventory.db"
    db.init_db(path)
    assert inventory(path) == {
        "WidgetA": (15, 250.0),
        "WidgetB": (10, 500.0),
        "GadgetX": (5, 750.0),
        "FakeItem": (0, None),
    }


def test_init_is_idempotent(tmp_path):
    path = tmp_path / "inventory.db"
    db.init_db(path)
    db.init_db(path)
    assert len(inventory(path)) == 4


def test_wal_mode(tmp_path):
    path = tmp_path / "inventory.db"
    db.init_db(path)
    with closing(db.connect(path)) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_reset_restores_seed(tmp_path):
    path = tmp_path / "inventory.db"
    db.init_db(path)
    with closing(db.connect(path)) as conn, conn:
        conn.execute("UPDATE inventory SET stock = 0 WHERE item = 'WidgetA'")
    db.init_db(path, reset=True)
    assert inventory(path)["WidgetA"] == (15, 250.0)
