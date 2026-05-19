"""Testa funções puras/helpers que não dependem de HTTP."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import app as _app

BRASILIA = ZoneInfo('America/Sao_Paulo')


def _user(status='trial', trial_days=3, ends_at=None):
    if ends_at is None and trial_days is not None:
        ends_at = (datetime.now(BRASILIA) + timedelta(days=trial_days)).strftime('%Y-%m-%d %H:%M:%S')
    return {'subscription_status': status, 'trial_ends_at': ends_at, 'subscription_ends_at': None}


# ── has_access ────────────────────────────────────────────────────────────────

class TestHasAccess:
    def test_none_user_returns_false(self):
        assert _app.has_access(None) is False

    def test_trial_active_returns_true(self):
        assert _app.has_access(_user(trial_days=2)) is True

    def test_trial_expired_returns_false(self):
        ended = (datetime.now(BRASILIA) - timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')
        assert _app.has_access(_user(ends_at=ended)) is False

    def test_ativo_without_end_date_returns_true(self):
        u = {'subscription_status': 'ativo', 'trial_ends_at': None, 'subscription_ends_at': None}
        assert _app.has_access(u) is True

    def test_ativo_with_future_end_date_returns_true(self):
        future = (datetime.now(BRASILIA) + timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')
        u = {'subscription_status': 'ativo', 'trial_ends_at': None, 'subscription_ends_at': future}
        assert _app.has_access(u) is True

    def test_ativo_with_past_end_date_returns_false(self):
        past = (datetime.now(BRASILIA) - timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')
        u = {'subscription_status': 'ativo', 'trial_ends_at': None, 'subscription_ends_at': past}
        assert _app.has_access(u) is False

    def test_legacy_user_without_trial_ends_at_returns_true(self):
        u = {'subscription_status': 'trial', 'trial_ends_at': None, 'subscription_ends_at': None}
        assert _app.has_access(u) is True

    def test_cancelado_with_expired_trial_returns_false(self):
        ended = (datetime.now(BRASILIA) - timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')
        u = {'subscription_status': 'cancelado', 'trial_ends_at': ended, 'subscription_ends_at': None}
        assert _app.has_access(u) is False


# ── dias_trial_restantes ──────────────────────────────────────────────────────

class TestDiasTrialRestantes:
    def test_returns_none_for_none_user(self):
        assert _app.dias_trial_restantes(None) is None

    def test_returns_none_for_ativo_user(self):
        u = {'subscription_status': 'ativo', 'trial_ends_at': None}
        assert _app.dias_trial_restantes(u) is None

    def test_returns_none_when_no_trial_ends_at(self):
        u = {'subscription_status': 'trial', 'trial_ends_at': None}
        assert _app.dias_trial_restantes(u) is None

    def test_returns_positive_days_when_trial_active(self):
        future = (datetime.now(BRASILIA) + timedelta(days=2)).strftime('%Y-%m-%d %H:%M:%S')
        u = {'subscription_status': 'trial', 'trial_ends_at': future}
        result = _app.dias_trial_restantes(u)
        assert result is not None and result >= 1

    def test_returns_zero_when_trial_expired(self):
        past = (datetime.now(BRASILIA) - timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')
        u = {'subscription_status': 'trial', 'trial_ends_at': past}
        assert _app.dias_trial_restantes(u) == 0


# ── r2_configured ─────────────────────────────────────────────────────────────

class TestR2Configured:
    def test_returns_false_without_env_vars(self, monkeypatch):
        monkeypatch.delenv('R2_ACCOUNT_ID', raising=False)
        monkeypatch.delenv('R2_ACCESS_KEY_ID', raising=False)
        monkeypatch.delenv('R2_BUCKET_NAME', raising=False)
        assert _app.r2_configured() is False

    def test_returns_true_with_all_env_vars(self, monkeypatch):
        monkeypatch.setenv('R2_ACCOUNT_ID', 'acc123')
        monkeypatch.setenv('R2_ACCESS_KEY_ID', 'key123')
        monkeypatch.setenv('R2_BUCKET_NAME', 'bucket')
        assert _app.r2_configured() is True

    def test_returns_false_with_partial_env_vars(self, monkeypatch):
        monkeypatch.setenv('R2_ACCOUNT_ID', 'acc123')
        monkeypatch.delenv('R2_ACCESS_KEY_ID', raising=False)
        monkeypatch.delenv('R2_BUCKET_NAME', raising=False)
        assert _app.r2_configured() is False
