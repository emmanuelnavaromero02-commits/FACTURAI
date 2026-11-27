"""The bank of invented portals: the real runner against portals it has never seen, without network.

The bank runs once for the whole class (two passes, scripted AI) and each test reads what it needs.
Portals the engine cannot solve yet stay in the bank; their tests pin the exact way they stop.
"""
import asyncio
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from facturador import banco
from facturador.auth import PlanError
from facturador.banco_portales import PORTALES, Vista
from facturador.banco_portales.portales import Cuenta, UnaPagina
from facturador.models import Plan
from simulated_portal import CHROME

FUERA = ('https://rastreo.sitio-ajeno.example/pixel.gif', 'https://fuentes.otro-sitio.example/letra.css')
CLAVES_IA = ('FACTURAI_OPENAI_API_KEY', 'FACTURAI_ANTHROPIC_API_KEY')


class Espia:
    """The scripted AI, keeping every text it was shown (to check nothing secret reaches the model)."""

    def __init__(self, plan):
        self.plan, self.textos = plan, []

    def status(self):
        return self.plan.status()

    def close(self):
        self.plan.close()

    def infer(self, instructions, content, output_type, **options):
        self.textos.append(instructions + '\n' + '\n'.join(part.get('text', '') for part in content))
        return self.plan.infer(instructions, content, output_type, **options)


class PortalConFugas(UnaPagina):
    """A bank portal whose first page also asks for an image and a stylesheet from other sites."""
    slug = 'con_fugas'
    marca = 'Cafetería de Prueba Fugas'
    origen = 'https://facturacion.cafeteriafugas.example'
    folio_formato = 'CF{n}'

    def responder(self, p):
        answer = super().responder(p)
        if p.ruta == '/' and answer is not None:
            answer.cuerpo = answer.cuerpo.replace(
                '</body>', f'<img src="{FUERA[0]}" alt=""><link rel="stylesheet" href="{FUERA[1]}"></body>')
        return answer


def correr(lista, plan=None, pasadas=2):
    with patch.dict(os.environ, {'FACTURADOR_CHROMIUM_PATH': CHROME}):
        return asyncio.run(banco.medir(banco=lista, plan=plan, rapido=True, tiempo_max=90, pasadas=pasadas))


class BancoSinNavegadorTests(unittest.TestCase):
    def test_banco_tiene_las_15_formas_con_marcas_y_dominios_inventados(self):
        portales = banco.portales()
        self.assertGreaterEqual(len(portales), 14)
        self.assertEqual(len({portal.slug for portal in portales}), len(portales))
        hosts = [host for portal in portales for host in portal.dominios]
        self.assertEqual(len(hosts), len(set(hosts)))
        self.assertTrue(all(host.endswith('.example') for host in hosts))
        self.assertTrue(all(origin.startswith('https://') for portal in portales for origin in portal.origenes))
        self.assertEqual({portal.esperado for portal in portales}, {'verificada', 'emitida_espera', 'persona'})
        self.assertEqual(sum(portal.esperado == 'persona' for portal in portales), 1)
        self.assertEqual(len(PORTALES), len(portales))

    def test_tickets_ficticios_distintos_por_pasada_y_validos(self):
        for portal in banco.portales():
            first, second = banco.ticket_para(portal, 1), banco.ticket_para(portal, 2)
            self.assertNotEqual(first.ticket_number, second.ticket_number)
            self.assertEqual(first, banco.ticket_para(portal, 1))
            self.assertEqual(first.ready_errors(), [])
            self.assertEqual(first.portal_url, portal.portal_url)

    def test_ia_guionizada_solo_decide_pantallas(self):
        ia = banco.IAGuionizada(banco.portales(['una_pagina']))
        self.assertEqual(ia.status()['status'], 'ready')
        with self.assertRaises(PlanError):
            ia.infer('x\n{}', [{'type': 'input_text', 'text': '{}'}], dict)
        plan = ia.infer('x\n' + json.dumps({'perfil': {}, 'ticket': {}, 'portal': ''}),
                        [{'type': 'input_text', 'text': json.dumps({'url': 'https://otro.example', 'campos': []})}], Plan)
        self.assertEqual(plan.steps[0].kind, 'human')

    def test_vista_lee_los_campos_numerados(self):
        view = Vista({'url': 'https://x.example', 'campos': ['#1 textbox «RFC»: vacío', '#2 combobox «Uso del CFDI»: G03'],
                      'estado_visible': '- heading "Datos"'},
                     {'rfc': 'XAXX010101000'}, {'amount': '10.00', 'date': '2026-11-02', 'ticket_number': 'A-1'})
        self.assertEqual(view.campo('^RFC')['n'], 1)
        self.assertEqual(view.del_tipo('combobox')[0]['etiqueta'], 'Uso del CFDI')

    def test_ia_real_sin_carpeta_de_datos_se_rechaza(self):
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            self.assertEqual(banco.main(['--ia-real']), 2)
        self.assertIn('--ia-real necesita --datos', error.getvalue())

    def test_ia_real_con_carpeta_sin_ia_lista_se_rechaza(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=False):
            for name in CLAVES_IA:
                os.environ.pop(name, None)
            error = io.StringIO()
            with contextlib.redirect_stderr(error):
                self.assertEqual(banco.main(['--ia-real', '--datos', folder, '--salida', str(Path(folder) / 'x.json')]), 2)
            self.assertIn('no tiene una IA lista', error.getvalue())
            self.assertFalse((Path(folder) / 'x.json').exists())
            # Its use is never counted in the folder that was checked.
            self.assertFalse((Path(folder) / 'facturador.sqlite').exists())

    def test_portal_desconocido_se_rechaza(self):
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            self.assertEqual(banco.main(['--portales', 'una_pagina,no_existe']), 2)
        self.assertIn('no_existe', error.getvalue())


@unittest.skipUnless(os.path.isfile(CHROME), 'Requiere Google Chrome oficial')
class BancoCompletoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.portales = banco.portales()
        cls.espia = Espia(banco.IAGuionizada(cls.portales))
        cls.resultado = correr(cls.portales, cls.espia)
        cls.por_slug = {portal.slug: portal for portal in cls.portales}

    def ticket(self, slug, pasada):
        return next(item for item in self.resultado['tickets'] if item['portal'] == slug and item['pasada'] == pasada)

    def test_cada_portal_llega_a_lo_esperado_o_a_su_falla_documentada(self):
        for portal in self.portales:
            for pasada, limitacion in ((1, portal.limitacion), (2, portal.limitacion or portal.limitacion_memoria)):
                with self.subTest(portal=portal.slug, pasada=pasada):
                    item = self.ticket(portal.slug, pasada)
                    if limitacion:
                        self.assertFalse(item['acierto'], 'Ya funciona: quita la marca «el motor todavía no puede».')
                    else:
                        self.assertTrue(item['acierto'], item['ultimos_eventos'])
                        self.assertEqual(item['peticiones_fuera_del_banco'], [])

    def test_cada_factura_comprobada_paso_por_el_cotejo(self):
        for item in self.resultado['tickets']:
            if item['obtenido'] == 'verificada':
                with self.subTest(portal=item['portal'], pasada=item['pasada']):
                    self.assertIn('PDF/XML coinciden', item['mensaje'])
                    self.assertEqual(item['estado'], 'verified')

    def test_segunda_pasada_repite_de_memoria_sin_consultar_antes_de_emitir(self):
        alone = [portal for portal in self.portales if portal.esperado != 'persona'
                 and not portal.limitacion and not portal.limitacion_memoria]
        learned = sum(self.ticket(portal.slug, 1)['pantallas_aprendidas'] for portal in alone)
        self.assertGreater(learned, len(alone))
        for portal in alone:
            with self.subTest(portal=portal.slug):
                first, second = self.ticket(portal.slug, 1), self.ticket(portal.slug, 2)
                self.assertGreater(second['acciones_repetidas'], 0)
                self.assertGreater(second['pantallas_repetidas'], 0)
                self.assertLessEqual(second['consultas'], first['consultas'])
                if portal.slug != 'redireccion_automatica':
                    # The page that forwards by itself is read again each time: nothing there to repeat.
                    self.assertEqual(second['consultas_preparatorias'], 0, second['ultimos_eventos'])
        second_pass = self.resultado['pasadas']['2']
        self.assertEqual(second_pass['mediana_consultas'], 0)

    def test_no_soy_un_robot_se_detiene_y_pide_a_la_persona(self):
        portal = self.por_slug['no_soy_robot']
        for pasada in (1, 2):
            item = self.ticket('no_soy_robot', pasada)
            self.assertTrue(item['acierto'])
            self.assertEqual(item['persona']['tipo'], 'captcha')
            self.assertEqual(item['consultas'], 0)
            self.assertEqual(item['acciones'], 0)
        # Nothing was sent: no lookup, no emission, only the page itself was requested.
        self.assertEqual(portal.consultas, [])
        self.assertEqual(portal.emisiones, {})
        self.assertTrue(all(method == 'GET' for method, _ in portal.peticiones))

    def test_ticket_ya_facturado_recupera_la_factura_existente(self):
        portal = self.por_slug['ya_facturado']
        for pasada in (1, 2):
            item = self.ticket('ya_facturado', pasada)
            self.assertEqual(item['obtenido'], 'verificada', item['ultimos_eventos'])
            self.assertEqual(item['intentos_de_emision'], 0)
        self.assertEqual(len(portal.consultas), 2)
        self.assertTrue(all(query['rfc'] == banco.PERFIL.rfc for query in portal.consultas))
        # Recovery never asked for a new invoice: only the two made before the run exist.
        self.assertEqual(portal.capturas, [])

    def test_por_correo_queda_emitida_y_se_comprueba_al_llegar_el_correo(self):
        for pasada in (1, 2):
            item = self.ticket('por_correo', pasada)
            self.assertEqual(item['obtenido'], 'emitida_espera')
            self.assertTrue(item['correo_entregado'])
            self.assertTrue(item['verificada_tras_correo'])

    def test_ninguna_peticion_sale_de_los_dominios_del_banco(self):
        self.assertEqual(self.resultado['peticiones_fuera_del_banco'], [])
        self.assertEqual(self.resultado['bloqueadas_por_el_motor'], [])
        for portal in self.portales:
            for method, url in portal.peticiones:
                self.assertTrue(url.startswith(portal.origenes), url)

    def test_la_ia_nunca_ve_la_contrasena_de_la_cuenta_creada(self):
        portal = self.por_slug['crear_cuenta']
        self.assertEqual(len(portal.cuentas), 1)
        password = next(iter(portal.cuentas.values()))
        self.assertGreaterEqual(len(password), 8)
        self.assertTrue(self.espia.textos)
        self.assertFalse(any(password in text for text in self.espia.textos))

    # -- what the engine cannot do yet: the exact failure, so a fix shows up here

    def test_falla_documentada_enlace_ir_a_facturar_se_trata_como_emision(self):
        portal = self.por_slug['redireccion_proveedor']
        self.assertTrue(portal.limitacion)
        for pasada in (1, 2):
            item = self.ticket('redireccion_proveedor', pasada)
            self.assertEqual(item['persona']['tipo'], 'issue')
            self.assertEqual(item['consultas'], 1)
            self.assertEqual(item['acciones'], 0)
        self.assertFalse(any(url.startswith(portal.otros_origenes[0]) for _, url in portal.peticiones))
        # The same provider shape is solved when the merchant page forwards by itself.
        self.assertTrue(self.ticket('redireccion_automatica', 1)['acierto'])
        self.assertGreaterEqual(self.ticket('redireccion_automatica', 1)['redirecciones'], 1)

    def test_falla_documentada_foto_del_ticket_nunca_llega_a_la_ia(self):
        portal = self.por_slug['foto_ticket']
        self.assertTrue(portal.limitacion)
        for pasada in (1, 2):
            item = self.ticket('foto_ticket', pasada)
            self.assertEqual(item['persona']['tipo'], 'attachment')
            self.assertEqual(item['consultas'], 0)
        self.assertFalse(any(method == 'POST' for method, _ in portal.peticiones))

    def test_falla_documentada_memoria_no_espera_a_las_opciones_tardias(self):
        portal = self.por_slug['opciones_tardias']
        self.assertTrue(portal.limitacion_memoria)
        self.assertTrue(self.ticket('opciones_tardias', 1)['acierto'])
        item = self.ticket('opciones_tardias', 2)
        self.assertEqual(item['persona']['tipo'], 'blocked')
        self.assertTrue(item['persona']['mensaje'].startswith('Antes de emitir, el portal debe reflejar'))
        self.assertEqual(item['consultas'], 0)
        self.assertGreater(item['acciones_repetidas'], 0)
        self.assertEqual(len(portal.emisiones), 1)

    def test_resumen_y_json(self):
        text = banco.resumen(self.resultado)
        first = self.resultado['pasadas']['1']
        self.assertIn(f"{first['salieron_solos']} de {first['pueden_salir_solos']} portales nuevos salieron solos", text)
        self.assertIn(f"Mediana: {first['mediana_segundos']} s y {first['mediana_consultas']} consultas", text)
        self.assertIn('Segunda pasada (con memoria)', text)
        self.assertIn('El motor todavía no puede: ', text)
        self.assertIn('Peticiones fuera del banco: ninguna.', text)
        data = json.loads(json.dumps(self.resultado, ensure_ascii=False))
        self.assertEqual(len(data['tickets']), 2 * len(self.portales))
        for item in data['tickets']:
            for field in ('esperado', 'obtenido', 'consultas', 'acciones', 'acciones_repetidas', 'segundos',
                          'consultas_preparatorias', 'pantallas_repetidas', 'pantallas_aprendidas'):
                self.assertIn(field, item)
        self.assertEqual(first['pueden_salir_solos'], len(self.portales) - 1)


@unittest.skipUnless(os.path.isfile(CHROME), 'Requiere Google Chrome oficial')
class AislamientoTests(unittest.TestCase):
    def test_peticion_fuera_del_banco_se_bloquea_y_cuenta_como_error_del_arnes(self):
        portal = PortalConFugas()
        result = correr([portal], pasadas=1)
        self.assertEqual(sorted(set(result['peticiones_fuera_del_banco'])), sorted(FUERA))
        self.assertEqual(sorted(set(result['tickets'][0]['peticiones_fuera_del_banco'])), sorted(FUERA))
        self.assertIn('error del arnés', banco.resumen(result))
        self.assertFalse(any(url.startswith(FUERA) for _, url in portal.peticiones))

    def test_comando_escribe_json_y_resumen(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'FACTURADOR_CHROMIUM_PATH': CHROME}):
            output = Path(folder) / 'medicion.json'
            printed = io.StringIO()
            with contextlib.redirect_stdout(printed):
                self.assertEqual(banco.main(['--portales', 'una_pagina', '--salida', str(output)]), 0)
            data = json.loads(output.read_text(encoding='utf-8'))
        self.assertEqual([item['portal'] for item in data['tickets']], ['una_pagina', 'una_pagina'])
        self.assertEqual(data['ia'], 'guionizada')
        self.assertIn('1 de 1 portales nuevos salieron solos', printed.getvalue())


if __name__ == '__main__':
    unittest.main()
