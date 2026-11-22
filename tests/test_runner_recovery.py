import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from facturador.db import Store
from facturador.documents import save_candidate
from facturador.runner import Runner
from test_core import FISCAL_UUID, PROFILE, RECEIPT, pdf_bytes, xml_bytes


class InterruptedDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.store = Store(Path(folder))
        self.runner = Runner(self.store)
        self.addAsyncCleanup(self.runner.close)
        row, _ = self.store.add('ticket.png', 'recuperacion')
        self.key = row['id']
        self.store.save_receipt(self.key, RECEIPT, ready=True)

    async def interrupted(self, complete):
        self.store.begin_emission(self.key, 'Facturar')
        save_candidate(self.store, self.key, 'factura.xml', xml_bytes())
        if complete:
            async def finishing():
                await asyncio.sleep(.01)
                save_candidate(self.store, self.key, 'factura.pdf', pdf_bytes())

            self.runner.downloads.add(asyncio.create_task(finishing()))
        raise ValueError('Se perdió el navegador mientras enviaba el comprobante.')

    async def run_with_interruption(self, complete):
        async def session(*_):
            await self.interrupted(complete)

        with patch.object(self.runner, '_session', side_effect=session) as merchant:
            self.runner.schedule(self.key, PROFILE)
            await self.runner.job_tasks[self.key]
            merchant.assert_awaited_once()
        self.assertFalse(self.store.queue(self.key, PROFILE))
        return self.store.get(self.key)

    async def test_in_flight_pair_finishes_without_reopening_the_merchant(self):
        row = await self.run_with_interruption(True)
        self.assertEqual((row['status'], row['uuid']), ('verified', FISCAL_UUID))
        self.assertEqual(Path(row['xml']).read_bytes(), xml_bytes())
        self.assertEqual(Path(row['pdf']).read_bytes(), pdf_bytes())

    async def test_one_file_preserves_uncertain_outcome_without_a_second_submission(self):
        row = await self.run_with_interruption(False)
        self.assertEqual(row['status'], 'uncertain')
        self.assertTrue(row['issue_attempted'])
        self.assertFalse(row['pdf'])
