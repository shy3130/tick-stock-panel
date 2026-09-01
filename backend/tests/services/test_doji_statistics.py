from app.services.doji_patterns.statistics import interaction_cluster_bootstrap


def test_interaction_bootstrap_requires_all_four_cells():
    r = interaction_cluster_bootstrap(
        {"600000.SH": [0.1]}, {"600000.SH": [0.0]}, {}, {"600000.SH": [0.2]}, rounds=20
    )
    assert r.valid_replicates == 0 and r.mean_difference is None


def test_interaction_bootstrap_is_deterministic_symbol_clustered():
    args = (
        {"600000.SH": [0.2], "000001.SZ": [0.1]},
        {"600000.SH": [0.0], "000001.SZ": [0.0]},
        {"600000.SH": [0.1], "000001.SZ": [0.1]},
        {"600000.SH": [0.0], "000001.SZ": [0.0]},
    )
    a = interaction_cluster_bootstrap(*args, rounds=50)
    b = interaction_cluster_bootstrap(*args, rounds=50)
    assert a == b and a.valid_replicates == 50
