from unittest.mock import MagicMock, patch

import pytest

from app.core import provider_store as store


@pytest.mark.parametrize('platform', ['darwin', 'linux'])
def test_native_store_encrypts_and_reloads_without_plaintext(tmp_path, monkeypatch, platform):
    monkeypatch.setenv('ORGANIZER_HOME', str(tmp_path))
    secrets = {}
    backend = MagicMock()
    backend.get_password.side_effect = lambda service, account: secrets.get((service, account))
    backend.set_password.side_effect = lambda service, account, key: secrets.__setitem__((service, account), key)
    with patch.object(store.sys, 'platform', platform), patch.object(store, '_native_keyring', return_value=backend):
        first = store._protect_secret('fixture-secret')
        second = store._protect_secret('second-fixture')
        assert first.startswith('keyring-fernet:')
        assert 'fixture-secret' not in first
        assert store._unprotect_secret(first) == 'fixture-secret'
        assert store._unprotect_secret(second) == 'second-fixture'
        backend.set_password.assert_called_once()
        assert list(tmp_path.iterdir()) == [], 'Encryption master key is never written to local files'


def test_locked_native_store_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv('ORGANIZER_HOME', str(tmp_path))
    with patch.object(store.sys, 'platform', 'linux'), patch.object(store, '_native_keyring', side_effect=RuntimeError('locked')):
        with pytest.raises(store.ProviderConfigurationError, match='not saved as plaintext'):
            store._protect_secret('fixture-secret')
    assert list(tmp_path.iterdir()) == []


def test_missing_native_key_does_not_generate_replacement(tmp_path, monkeypatch):
    monkeypatch.setenv('ORGANIZER_HOME', str(tmp_path))
    backend = MagicMock()
    backend.get_password.return_value = None
    with patch.object(store.sys, 'platform', 'darwin'), patch.object(store, '_native_keyring', return_value=backend):
        with pytest.raises(store.ProviderConfigurationError):
            store._unprotect_secret('keyring-fernet:invalid')
    backend.set_password.assert_not_called()


def test_windows_credentials_are_not_silently_reinterpreted_on_mac():
    with patch.object(store.sys, 'platform', 'darwin'):
        with pytest.raises(store.ProviderConfigurationError, match='another platform'):
            store._unprotect_secret('a-Windows-DPAPI-ciphertext')
