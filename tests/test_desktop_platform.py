"""Platform folder-opening commands keep a path in a single argument."""
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app import api


@pytest.mark.parametrize('platform,opener', [('win32', 'explorer.exe'), ('darwin', 'open'), ('linux', 'xdg-open')])
def test_open_folder_uses_native_file_manager(tmp_path, platform, opener):
    folder = tmp_path / 'A folder with spaces'
    folder.mkdir()
    with patch.object(api.sys, 'platform', platform), patch.object(api.subprocess, 'Popen') as launch:
        assert api.open_folder(api.UndoRequest(source=str(folder))) == {'opened': True}
    launch.assert_called_once_with([opener, str(folder.resolve())])


def test_open_folder_reports_missing_file_manager(tmp_path):
    with patch.object(api.subprocess, 'Popen', side_effect=FileNotFoundError('opener unavailable')):
        with pytest.raises(HTTPException) as error:
            api.open_folder(api.UndoRequest(source=str(tmp_path)))
    assert error.value.status_code == 500
    assert 'Could not open the folder' in error.value.detail


def test_open_folder_does_not_launch_for_missing_directory(tmp_path):
    with patch.object(api.subprocess, 'Popen') as launch:
        with pytest.raises(HTTPException) as error:
            api.open_folder(api.UndoRequest(source=str(tmp_path / 'missing')))
    assert error.value.status_code == 400
    launch.assert_not_called()
