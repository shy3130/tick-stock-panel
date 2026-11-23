from pathlib import Path


def test_installer_delegates_previous_and_explicit_directories_to_inno():
    script = (Path(__file__).resolve().parents[2] / "packaging" / "tsp.iss").read_text(encoding="utf-8")
    assert "UsePreviousAppDir=yes" in script
    assert "DefaultDirName={code:GetDefaultInstallDir}" in script
    assert "WizardForm.DirEdit.Text :=" not in script
    assert 'Excludes: "\\data,\\data\\*"' in script
    assert "AppId={{B7F4C9E2-3A51-4D8E-9C60-5E8A2F1D7B43}" in script
