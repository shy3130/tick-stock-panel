from pathlib import Path


def test_backup_task_registration_uses_two_safe_triggers_and_single_instance() -> None:
    project_root = Path(__file__).resolve().parents[2]
    script = (project_root / "scripts" / "register-tickflow-backup.ps1").read_text(
        encoding="utf-8"
    )

    assert "Monday, Tuesday, Wednesday, Thursday, Friday" in script
    assert '19:00' in script
    assert "Sunday" in script
    assert '03:00' in script
    assert "-MultipleInstances IgnoreNew" in script
    assert "-ExecutionTimeLimit (New-TimeSpan -Hours 2)" in script
    assert "backend\\.venv\\Scripts\\python.exe" in script
    assert "backend\\scripts\\tickflow_backup.py" in script
    assert "Register-ScheduledTask" in script
    assert "AUTH_PASSWORD" not in script
    assert "API_KEY" not in script


def test_restore_smoke_script_uses_isolated_bind_and_never_overwrites_production() -> None:
    project_root = Path(__file__).resolve().parents[2]
    script = (project_root / "scripts" / "verify-tickflow-restore.ps1").read_text(
        encoding="utf-8"
    )

    assert "restore-test" in script
    assert "restore-test" in script.lower()
    assert "127.0.0.1:$Port`:3018" in script
    assert "tickflow_backup.py" in script
    assert "production-data" in script
    assert "Invoke-WebRequest" in script
    assert "auth.json" in script
    assert "secrets.json" in script
    assert "Remove-Item -LiteralPath $RestoreDir -Recurse" in script
