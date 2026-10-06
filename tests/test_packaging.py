from pathlib import Path
import subprocess


def test_package_stages_current_source_with_relocatable_bytecode(tmp_path):
    root = Path(__file__).resolve().parents[1]
    staged = tmp_path / "package"
    subprocess.run(
        [
            "bash",
            "-eu",
            "-c",
            'source "$1/PKGBUILD"; srcdir="$2/source"; pkgdir="$2/package"; prepare; package',
            "package-test",
            str(root),
            str(staged),
        ],
        check=True,
    )
    staged = staged / "package"
    target = staged / "usr/share/tracker-application"
    for path in [root / "main.py", *sorted((root / "app").glob("*.py"))]:
        assert (target / path.relative_to(root)).read_bytes() == path.read_bytes()
    assert list(target.rglob("*.pyc"))
    import marshal

    bytecode = next((target / "app/__pycache__").glob("ui.*.pyc"))
    code = marshal.loads(bytecode.read_bytes()[16:])
    assert code.co_filename == "/usr/share/tracker-application/app/ui.py"
    assert (staged / "usr/bin/tracker-application").stat().st_mode & 0o111
    assert (staged / "usr/share/licenses/tracker-application/LICENSE").is_file()
