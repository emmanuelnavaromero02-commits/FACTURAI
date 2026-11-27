"""The selector plugged into the real panel behaves like today's plan and its routes are guarded."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from facturador import app as panel
from facturador.auth import ChatGPTPlan
from facturador.ia_ajustes import ia_router
from facturador.medicion import medicion_router
from facturador.proveedores_ia import SelectorIA


class PanelWithSelector(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        plan = ChatGPTPlan(root)
        self.expected = plan.status()
        plan.close()
        # What the coordinator wires in app.py: the selector instead of ChatGPTPlan, plus both routers.
        with patch.object(panel, 'ChatGPTPlan', lambda data, port: SelectorIA(data, port, environ={})):
            self.app = panel.create_app(root)
        self.app.include_router(ia_router(root, self.app.state.plan))
        self.app.include_router(medicion_router(self.app.state.store))
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.token = self.client.get('/api/state').json()['token']

    def test_state_and_routes(self):
        self.assertIsInstance(self.app.state.plan, SelectorIA)
        self.assertIs(self.app.state.runner.plan, self.app.state.plan)
        self.assertEqual(self.client.get('/api/state').json()['plan'], self.expected)
        self.assertEqual(self.client.get('/api/ia').json()['activo'], 'chatgpt')
        self.assertEqual(self.client.put('/api/ia/proveedor', json={'proveedor': 'chatgpt'}).status_code, 403)
        chosen = self.client.put('/api/ia/proveedor', json={'proveedor': 'chatgpt'}, headers={'x-app-token': self.token})
        self.assertEqual(chosen.status_code, 200)
        refused = self.client.put('/api/ia/proveedor', json={'proveedor': 'anthropic'}, headers={'x-app-token': self.token})
        self.assertEqual(refused.status_code, 422)
        self.assertEqual(self.client.get('/api/medicion/resumen?periodo=todo').json()['subidos'], 0)


if __name__ == '__main__':
    unittest.main()
