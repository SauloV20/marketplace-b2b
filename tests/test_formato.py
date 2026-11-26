"""Formatos brasileiros compartilhados (formato.py): dinheiro, números digitados, datas de Brasília e tamanhos."""
import unittest
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import formato


class TesteFormato(unittest.TestCase):
    def test_dinheiro_e_numeros(self):
        self.assertEqual(formato.brl(Decimal("1234.5")), "R$ 1.234,50")
        self.assertEqual(formato.brl(0), "R$ 0,00")
        self.assertEqual((formato.num(Decimal("12.50")), formato.num(None)), ("12,5", ""))
        self.assertEqual((formato.duas_casas(12.5), formato.uma_casa(12.34)), ("12,50", "12,3"))

    def test_ler_numero(self):
        casos = {"1.234,56": "1234.56", "R$ 18,90": "18.90", "10%": "10", "4.5": "4.5", " 7 ": "7"}
        for texto, esperado in casos.items():
            self.assertEqual(formato.ler_numero(texto), Decimal(esperado), texto)
        self.assertEqual(formato.ler_numero(0.35), Decimal("0.35"))  # célula do Excel
        self.assertEqual(formato.ler_numero("", vazio=0), 0)
        self.assertEqual(formato.ler_numero("12,348", casas=formato.CENTAVO), Decimal("12.35"))
        for ruim in ("", "abc", "inf", "NaN"):
            with self.assertRaises(InvalidOperation, msg=ruim):
                formato.ler_numero(ruim)

    def test_decimal_ou_e_arredondar(self):
        self.assertEqual(formato.decimal_ou("abc", 3), Decimal(3))
        self.assertEqual(formato.decimal_ou(None), 0)
        self.assertEqual(formato.decimal_ou("0.3"), Decimal("0.3"))
        self.assertEqual(formato.arredondar(Decimal("2.345")), Decimal("2.35"))

    def test_datas_de_brasilia(self):
        utc = datetime(2026, 11, 24, 2, 30)  # 23h30 do dia 23 em Brasília
        self.assertEqual(formato.hora_br(utc), "23/11/2026 23:30")
        self.assertEqual(formato.dia_br(utc), "23/11/2026")
        self.assertEqual(formato.data_br(utc), date(2026, 11, 23))
        self.assertEqual(formato.inicio_do_dia(date(2026, 11, 24)), datetime(2026, 11, 24, 3, 0))
        self.assertEqual((formato.hora_br(None), formato.dia_br(None)), ("", ""))

    def test_campo_e_tamanho(self):
        self.assertEqual(formato.campo({"n": "  Rua   das  Flores "}, "n"), "Rua das Flores")
        self.assertEqual(formato.campo({}, "n"), "")
        self.assertEqual((formato.tamanho(2_500_000), formato.tamanho(1500), formato.tamanho(0)), ("2,5 MB", "1 KB", "0 KB"))


if __name__ == "__main__":
    unittest.main()
