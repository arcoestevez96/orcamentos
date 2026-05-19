"""Testa extrair_valor_pdf — usa o stub de pdfplumber registrado em conftest."""
import sys
from unittest.mock import patch, MagicMock

import app as _app

_FAKE_PDF = b'%PDF-1.4 fake content'


def _configure_pdf_mock(text):
    """Configura o stub global de pdfplumber para retornar o texto dado."""
    page = MagicMock()
    page.extract_text.return_value = text
    pdf_obj = MagicMock()
    pdf_obj.pages = [page]
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=pdf_obj)
    ctx.__exit__ = MagicMock(return_value=False)
    sys.modules['pdfplumber'].open.return_value = ctx


def _configure_pdf_mock_multipages(texts):
    pages = []
    for t in texts:
        p = MagicMock()
        p.extract_text.return_value = t
        pages.append(p)
    pdf_obj = MagicMock()
    pdf_obj.pages = pages
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=pdf_obj)
    ctx.__exit__ = MagicMock(return_value=False)
    sys.modules['pdfplumber'].open.return_value = ctx


def extract(text, *, api_key=''):
    _configure_pdf_mock(text)
    with patch.dict('os.environ', {'ANTHROPIC_API_KEY': api_key}):
        return _app.extrair_valor_pdf(_FAKE_PDF)


class TestRegexExtraction:
    def test_valor_total_simples(self):
        assert extract('VALOR TOTAL R$ 1.500,00') == 1500.0

    def test_total_geral(self):
        assert extract('Serviço\nTOTAL GERAL 2.350,25') == 2350.25

    def test_total_a_pagar(self):
        assert extract('TOTAL A PAGAR 890,00') == 890.0

    def test_total_do_orcamento(self):
        assert extract('TOTAL DO ORÇAMENTO R$ 45.000,00') == 45000.0

    def test_grand_total(self):
        assert extract('Subtotal 500,00\nGRAND TOTAL 1.200,00') == 1200.0

    def test_valor_com_milhares(self):
        assert extract('VALOR TOTAL R$ 147.269,58') == 147269.58

    def test_prefere_valor_total_sobre_subtotal(self):
        text = 'Subtotal R$ 500,00\nDesconto R$ 50,00\nVALOR TOTAL R$ 450,00'
        assert extract(text) == 450.0

    def test_prioriza_valor_total_sobre_total_generico(self):
        text = 'TOTAL 800,00\nVALOR TOTAL 600,00'
        assert extract(text) == 600.0

    def test_remove_espacos_entre_digitos(self):
        # PDFs problemáticos inserem espaços: "1 47.269,58" → "147.269,58"
        assert extract('VALOR TOTAL 1 47.269,58') == 147269.58

    def test_fallback_maior_valor_sem_keyword(self):
        text = 'Mão de obra R$ 200,00\nMateriais R$ 800,00\nFrete R$ 50,00'
        assert extract(text) == 800.0

    def test_pdf_sem_texto_retorna_zero(self):
        assert extract('') == 0.0

    def test_pdf_sem_valores_monetarios_retorna_zero(self):
        assert extract('Orçamento sem valores numéricos para produtos.') == 0.0

    def test_multiplas_paginas(self):
        _configure_pdf_mock_multipages(['Item A R$ 300,00', 'VALOR TOTAL R$ 300,00'])
        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': ''}):
            result = _app.extrair_valor_pdf(_FAKE_PDF)
        assert result == 300.0

    def test_sem_api_key_nao_chama_claude(self):
        _configure_pdf_mock('VALOR TOTAL R$ 100,00')
        mock_anthropic = MagicMock()
        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': ''}), \
             patch.dict(sys.modules, {'anthropic': mock_anthropic}):
            _app.extrair_valor_pdf(_FAKE_PDF)
        mock_anthropic.Anthropic.assert_not_called()

    def test_erro_no_pdfplumber_retorna_zero(self):
        sys.modules['pdfplumber'].open.side_effect = Exception('PDF corrompido')
        result = _app.extrair_valor_pdf(_FAKE_PDF)
        sys.modules['pdfplumber'].open.side_effect = None
        assert result == 0.0


class TestAIExtraction:
    def test_usa_resultado_da_ia_quando_valido(self):
        _configure_pdf_mock('VALOR TOTAL R$ 2.500,00')
        mock_msg = MagicMock()
        mock_msg.content = [MagicMock(text='2.500,00')]
        mock_client_inst = MagicMock()
        mock_client_inst.messages.create.return_value = mock_msg
        mock_anthropic = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client_inst

        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'fake-key'}), \
             patch.dict(sys.modules, {'anthropic': mock_anthropic}):
            result = _app.extrair_valor_pdf(_FAKE_PDF)
        assert result == 2500.0

    def test_fallback_para_regex_quando_ia_falha(self):
        _configure_pdf_mock('VALOR TOTAL R$ 1.800,00')
        mock_client_inst = MagicMock()
        mock_client_inst.messages.create.side_effect = Exception('API indisponível')
        mock_anthropic = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client_inst

        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'fake-key'}), \
             patch.dict(sys.modules, {'anthropic': mock_anthropic}):
            result = _app.extrair_valor_pdf(_FAKE_PDF)
        assert result == 1800.0

    def test_fallback_quando_ia_retorna_zero(self):
        _configure_pdf_mock('VALOR TOTAL R$ 950,00')
        mock_msg = MagicMock()
        mock_msg.content = [MagicMock(text='0')]
        mock_client_inst = MagicMock()
        mock_client_inst.messages.create.return_value = mock_msg
        mock_anthropic = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client_inst

        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'fake-key'}), \
             patch.dict(sys.modules, {'anthropic': mock_anthropic}):
            result = _app.extrair_valor_pdf(_FAKE_PDF)
        assert result == 950.0
