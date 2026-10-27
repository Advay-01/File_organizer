import errno
from pathlib import Path
from unittest.mock import patch

import pytest

from app.core import safety


def test_native_move_never_overwrites_an_existing_file(tmp_path):
    source = tmp_path / 'source.txt'
    destination = tmp_path / 'destination.txt'
    source.write_text('source contents', encoding='utf-8')
    destination.write_text('keep existing contents', encoding='utf-8')
    with pytest.raises(FileExistsError):
        safety.move_file_no_overwrite(source, destination)
    assert source.read_text(encoding='utf-8') == 'source contents'
    assert destination.read_text(encoding='utf-8') == 'keep existing contents'


@pytest.mark.parametrize('code', [errno.EXDEV, errno.ENOSYS, errno.ENOTSUP])
def test_unsupported_native_rename_uses_exclusive_copy(tmp_path, code):
    source, destination = tmp_path / 'source.txt', tmp_path / 'destination.txt'
    source.write_text('original', encoding='utf-8')
    with patch.object(safety, '_rename_no_replace', side_effect=OSError(code, 'fixture')):
        safety.move_file_no_overwrite(source, destination)
    assert destination.read_text(encoding='utf-8') == 'original'
    assert not source.exists()


def test_copy_fallback_does_not_replace_a_destination_created_during_the_move(tmp_path):
    source, destination = tmp_path / 'source.txt', tmp_path / 'destination.txt'
    source.write_text('original', encoding='utf-8')
    def race(*args):
        destination.write_text('new arrival', encoding='utf-8')
        raise OSError(errno.EXDEV, 'fixture')
    with patch.object(safety, '_rename_no_replace', side_effect=race):
        with pytest.raises(FileExistsError):
            safety.move_file_no_overwrite(source, destination)
    assert source.read_text(encoding='utf-8') == 'original'
    assert destination.read_text(encoding='utf-8') == 'new arrival'


def test_system_ancestor_alias_resolves_to_the_same_approved_directory(tmp_path):
    real = tmp_path / 'real'
    root = real / 'work'
    root.mkdir(parents=True)
    alias = tmp_path / 'system-alias'
    try:
        alias.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip('Creating a directory symlink is unavailable')
    source = alias / 'work/source.txt'
    source.write_text('original', encoding='utf-8')
    safety.validate_move_paths(source, alias / 'work/Docs/Notes/source.txt', root)
    nested = root / 'redirect'
    nested.symlink_to(root, target_is_directory=True)
    with pytest.raises(safety.UnsafePathError):
        safety.validate_move_paths(source, nested / 'source.txt', root)
