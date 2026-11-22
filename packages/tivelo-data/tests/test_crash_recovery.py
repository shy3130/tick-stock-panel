import os
import subprocess
import sys

import pytest
from tivelo_contracts import ErrorCode, TiveloError

from tivelo_data.storage import SnapshotStore


def test_d08_real_process_exit_before_index_commit_leaves_no_published_version(
    make_bar, make_engine
):
    engine, _, plan = make_engine((make_bar(),))
    candidate = engine.sync(plan)
    candidate_path = engine.store.path(f"staging/{candidate.candidate_id}/candidate.json")
    program = """
import os, sys
from pathlib import Path
from tivelo_data.storage import SnapshotStore, Candidate
store = SnapshotStore(sys.argv[1])
candidate = Candidate.model_validate_json(Path(sys.argv[2]).read_bytes())
original = store.write_atomic
def crash(relative, content):
    original(relative, content)
    if relative.startswith('dataset_versions/'):
        os._exit(23)
store.write_atomic = crash
store.publish(candidate)
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(engine.store.root), str(candidate_path)],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
        timeout=30,
    )
    assert result.returncode == 23, result.stderr
    restarted = SnapshotStore(engine.store.root)
    with pytest.raises(TiveloError) as raised:
        restarted.dataset_version(candidate.version.version_id)
    assert raised.value.code == ErrorCode.SNAPSHOT_NOT_FOUND
    assert engine.publish(candidate) == candidate.version
