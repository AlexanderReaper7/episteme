import time
from pathlib import Path

from episteme.config import settings
from episteme.worker import backup


def test_dump_command_uses_custom_format_and_zstd(monkeypatch):
    monkeypatch.setattr(settings, "backup_zstd_level", 9)
    dest = Path("/backups/episteme-20260720-050000.dump")
    cmd = backup.dump_command("postgresql://u:p@db:5432/episteme", dest)

    assert cmd[0] == "pg_dump"
    assert "--format=custom" in cmd
    assert "--compress=zstd:9" in cmd
    # --file and --dbname are passed as separate argv tokens (value follows flag).
    assert cmd[cmd.index("--file") + 1] == str(dest)
    assert cmd[cmd.index("--dbname") + 1] == "postgresql://u:p@db:5432/episteme"


def test_prune_removes_only_expired_dumps(tmp_path):
    old = tmp_path / "episteme-20200101-000000.dump"
    fresh = tmp_path / "episteme-20260720-000000.dump"
    unrelated = tmp_path / "notes.txt"
    for p in (old, fresh, unrelated):
        p.write_text("x")

    # Backdate `old` well beyond the retention window.
    ancient = time.time() - 30 * 86400
    import os

    os.utime(old, (ancient, ancient))

    removed = backup.prune_old_backups(tmp_path, retention_days=14)

    assert removed == 1
    assert not old.exists()
    assert fresh.exists()
    assert unrelated.exists()  # non-dump files are never touched


def test_prune_disabled_when_retention_non_positive(tmp_path):
    old = tmp_path / "episteme-20200101-000000.dump"
    old.write_text("x")
    ancient = time.time() - 999 * 86400
    import os

    os.utime(old, (ancient, ancient))

    assert backup.prune_old_backups(tmp_path, retention_days=0) == 0
    assert old.exists()
