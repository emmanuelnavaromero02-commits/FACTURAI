"""Registration email round trip through the API and an offline Google Chrome form."""
import asyncio
from email.message import EmailMessage
from pathlib import Path
import tempfile

import httpx

from facturador.app import create_app


async def main():
    origin = "https://registro.example.com"
    profile = dict(name="PRUEBA PRIVADA", rfc="XAXX010101000", postal_code="22000",
                   regime="616", email="persona@example.net", cfdi_use="S01")
    with tempfile.TemporaryDirectory() as folder:
        app = create_app(Path(folder))
        runner = app.state.runner
        try:
            await runner.start_browser()
            page = await runner.context.new_page()
            await page.route("**/*", lambda route: route.fulfill(content_type="text/html; charset=utf-8",
                             body='<form><label>Contraseña actual<input type="password" autocomplete="current-password"></label></form>'))
            await page.goto(origin)
            await runner.viewer.show(page, "Registro de prueba", manual=True)
            headers = {"X-App-Token": app.state.csrf_token, "Origin": "http://127.0.0.1:8765"}
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765", headers=headers) as client:
                (await client.put("/api/profile", json=profile)).raise_for_status()
                revision = runner.viewer.revision
                channel = (await client.post("/api/browser/control", json={"revision": revision})).json()["channel"]
                args = {"origin": origin, "revision": revision, "channel": channel}
                msg = EmailMessage()
                msg["From"], msg["To"] = "cuentas@registro.example.com", profile["email"]
                msg.set_content("Contraseña temporal: Ejemplo-834!\nhttps://registro.example.com/activar?clave=demo")
                result = await client.post("/api/portal-mail/file", data=args, files={"file":("demo.eml", msg.as_bytes())})
                result.raise_for_status()
                assert set(result.json()["actions"]) == {"temporary","activate"}
                assert "Ejemplo" not in result.text and "clave=" not in result.text
                result = await client.post("/api/portal-mail/apply", json={**args,"choice":"temporary"})
                result.raise_for_status()
                assert await page.get_by_label("Contraseña actual").input_value() == "Ejemplo-834!"
                result = await client.post("/api/portal-mail/apply", json={**args,"choice":"temporary"})
                assert result.status_code == 422
                result = await client.post("/api/portal-mail/apply", json={**args,"choice":"activate"})
                result.raise_for_status()
                assert page.url == origin + "/activar?clave=demo"
                print("Correo del portal: selección, clave temporal de un uso y activación en el mismo dominio verificadas.")
        finally:
            await runner.close()


if __name__ == "__main__":
    asyncio.run(main())
