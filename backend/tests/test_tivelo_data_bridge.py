"""Existing v1 consumers resolve the extracted implementation, without forks."""

import subprocess
import sys


def test_legacy_provider_and_normalizer_imports_share_the_package_objects():
    from tivelo_data.legacy import base as extracted_base
    from tivelo_data.legacy import normalizer as extracted_normalizer
    from tivelo_data.legacy.frame import filter_halt_days

    from app.data_providers import base, normalizer
    from app.indicators import pipeline

    assert base is extracted_base
    assert normalizer is extracted_normalizer
    assert pipeline.filter_halt_days is filter_halt_days
    assert normalizer.filter_halt_days is filter_halt_days


def test_legacy_normalization_no_longer_imports_application_settings_or_indicators():
    code = (
        "import sys; import tivelo_data.legacy.normalizer; "
        "assert not any(n == 'app' or n.startswith('app.') for n in sys.modules); "
        "assert 'tivelo_data.engine' not in sys.modules"
    )
    subprocess.run([sys.executable, "-I", "-c", code], check=True, capture_output=True, text=True)
