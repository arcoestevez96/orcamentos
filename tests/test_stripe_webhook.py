"""Testa o webhook do Stripe sem depender de credenciais reais.

O handler EXIGE STRIPE_WEBHOOK_SECRET (rejeita eventos não assinados com 503).
Os testes setam um secret fake e mockam stripe.Webhook.construct_event para
exercitar o caminho verificado.
"""
import json
from unittest.mock import patch
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from tests.conftest import make_user

BRASILIA = ZoneInfo('America/Sao_Paulo')

WEBHOOK_URL = '/webhook/stripe'


def _post_event(client, event_type, obj_data):
    # Com secret + assinatura verificada (construct_event mockado retorna o evento)
    event = {'type': event_type, 'data': {'object': obj_data}}
    with patch.dict('os.environ', {'STRIPE_WEBHOOK_SECRET': 'whsec_test', 'STRIPE_SECRET_KEY': 'sk_test_fake'}), \
         patch('stripe.Webhook.construct_event', return_value=event):
        return client.post(WEBHOOK_URL, json={'type': event_type},
                           headers={'Stripe-Signature': 't=1,v1=fake'},
                           content_type='application/json')


class TestCheckoutCompleted:
    def test_ativa_assinatura_do_usuario(self, client):
        import app as _app
        make_user(email='pagante@teste.com', status='trial')

        obj = {
            'metadata': {'email': 'pagante@teste.com', 'plano': 'mensal'},
            'payment_status': 'paid',
            'customer_email': 'pagante@teste.com',
        }
        resp = _post_event(client, 'checkout.session.completed', obj)
        assert resp.status_code == 200
        u = _app.get_user_by_email('pagante@teste.com')
        assert u['subscription_status'] == 'ativo'
        assert u['subscription_ends_at'] is not None

    def test_plano_anual_define_365_dias(self, client):
        import app as _app
        make_user(email='anual@teste.com', status='trial')

        obj = {
            'metadata': {'email': 'anual@teste.com', 'plano': 'anual'},
            'payment_status': 'paid',
            'customer_email': 'anual@teste.com',
        }
        _post_event(client, 'checkout.session.completed', obj)
        u = _app.get_user_by_email('anual@teste.com')
        ends = datetime.strptime(u['subscription_ends_at'][:10], '%Y-%m-%d')
        dias = (ends - datetime.now()).days
        assert dias >= 364

    def test_payment_status_unpaid_nao_ativa(self, client):
        import app as _app
        make_user(email='unpaid@teste.com', status='trial')

        obj = {
            'metadata': {'email': 'unpaid@teste.com', 'plano': 'mensal'},
            'payment_status': 'unpaid',
        }
        _post_event(client, 'checkout.session.completed', obj)
        u = _app.get_user_by_email('unpaid@teste.com')
        assert u['subscription_status'] == 'trial'

    def test_email_vazio_nao_causa_erro(self, client):
        obj = {'metadata': {}, 'payment_status': 'paid', 'customer_email': ''}
        resp = _post_event(client, 'checkout.session.completed', obj)
        assert resp.status_code == 200


class TestSubscriptionDeleted:
    def test_cancela_assinatura(self, client):
        import app as _app
        make_user(email='cancela@teste.com', status='ativo')

        obj = {'customer_email': 'cancela@teste.com', 'customer': 'cus_fake'}
        resp = _post_event(client, 'customer.subscription.deleted', obj)
        assert resp.status_code == 200
        u = _app.get_user_by_email('cancela@teste.com')
        assert u['subscription_status'] == 'cancelado'

    def test_paused_tambem_cancela(self, client):
        import app as _app
        make_user(email='pausa@teste.com', status='ativo')

        obj = {'customer_email': 'pausa@teste.com', 'customer': 'cus_fake'}
        resp = _post_event(client, 'customer.subscription.paused', obj)
        assert resp.status_code == 200
        u = _app.get_user_by_email('pausa@teste.com')
        assert u['subscription_status'] == 'cancelado'


class TestWebhookSeguranca:
    def test_sem_secret_retorna_503(self, client):
        # Brecha fechada: sem STRIPE_WEBHOOK_SECRET o evento é REJEITADO (não ativa assinatura)
        with patch.dict('os.environ', {'STRIPE_WEBHOOK_SECRET': '', 'STRIPE_SECRET_KEY': 'sk_test'}):
            resp = client.post(WEBHOOK_URL, data=b'{"type":"checkout.session.completed"}',
                               content_type='application/json')
        assert resp.status_code == 503

    def test_payload_invalido_retorna_400(self, client):
        with patch.dict('os.environ', {'STRIPE_WEBHOOK_SECRET': 'whsec_test', 'STRIPE_SECRET_KEY': 'sk_test'}), \
             patch('stripe.Webhook.construct_event', side_effect=Exception('payload inválido')):
            resp = client.post(WEBHOOK_URL,
                               data=b'nao-e-json',
                               content_type='application/json',
                               headers={'Stripe-Signature': 'invalida'})
        assert resp.status_code == 400

    def test_com_webhook_secret_assinatura_invalida_retorna_400(self, client):
        import stripe
        with patch.dict('os.environ', {'STRIPE_WEBHOOK_SECRET': 'whsec_test', 'STRIPE_SECRET_KEY': 'sk_test'}), \
             patch('stripe.Webhook.construct_event',
                   side_effect=stripe.error.SignatureVerificationError('bad sig', 'sig')):
            resp = client.post(WEBHOOK_URL,
                               data=b'{"type":"test"}',
                               content_type='application/json',
                               headers={'Stripe-Signature': 'invalida'})
        assert resp.status_code == 400

    def test_evento_desconhecido_retorna_200(self, client):
        resp = _post_event(client, 'customer.unknown_event', {})
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data['ok'] is True
