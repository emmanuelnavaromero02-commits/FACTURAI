# Puerta: tu panel de una sola persona desde otro aparato

El modo local de FACTURAI no tiene cuentas: quien llega al panel lo maneja, y por eso solo escucha en `127.0.0.1`. Esta puerta permite abrir ese mismo panel desde el celular sin cambiarlo de modo y sin perder tus datos ni tus conexiones.

## Cómo funciona

- Un proxy (Caddy) recibe las visitas por `https` en un puerto público y las pasa primero por la puerta (`puerta.py`).
- Un aparato sin permiso no llega al panel: ve un número de 8 dígitos. Tú lo dictas a quien administra el servidor y esa persona lo aprueba desde la terminal.
- No hay contraseñas. El aparato aprobado guarda una llave larga en una cookie (`__Host-`, solo por `https`); en el servidor solo queda su huella.
- El permiso se pierde si lo quitas, o solo, tras 180 días sin usar ese aparato.
- Si la puerta falla, nadie pasa.

## Instalación (Linux con systemd)

1. Copia `puerta.py`, `Caddyfile` y el ejecutable de Caddy 2 a `/opt/facturai-puerta/`.
2. Copia los cuatro archivos `facturai-puerta*.service` y `.timer` a `/etc/systemd/system/`.
3. Escribe en `/etc/facturai-puerta.env` el nombre y el puerto por los que se entra:

   ```
   FACTURAI_PUERTA_SITIO=facturas.ejemplo.com:8443
   ```

   El puerto debe ser 1024 o mayor y el mismo por fuera y por dentro (sin redirigir el 443 a otro): el proxy corre sin privilegios y compara con ese valor el origen de cada petición del panel.

4. Indica el certificado de ese nombre en `/etc/systemd/system/facturai-puerta-proxy.service.d/certificado.conf`:

   ```
   [Service]
   LoadCredential=cert:/ruta/al/certificado.crt
   LoadCredential=key:/ruta/a/la/llave.key
   ```

5. Arranca y abre ese puerto en el cortafuegos:

   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now facturai-puerta facturai-puerta-proxy facturai-puerta-certificado.timer
   ```

El panel debe seguir en `127.0.0.1:8765`; la puerta usa `127.0.0.1:8766`. Cada domingo el proxy vuelve a leer el certificado, por si se renovó.

## Uso

```bash
sudo python3 /opt/facturai-puerta/puerta.py pendientes                  # aparatos que esperan permiso
sudo python3 /opt/facturai-puerta/puerta.py aprobar 12345678 "celular"  # el número que ve ese aparato
sudo python3 /opt/facturai-puerta/puerta.py lista                       # aparatos con permiso
sudo python3 /opt/facturai-puerta/puerta.py quitar a1b2c3               # quita el permiso
```

Aprueba solo el número que te dicte el dueño: el número identifica al aparato que lo muestra, y no sirve de nada a quien lo escuche.

## Lo que hay que saber

- **Conectar ChatGPT o Gmail se sigue haciendo en la máquina del panel.** Esas conexiones regresan a `127.0.0.1`; lo ya conectado funciona igual desde el celular.
- **Las ligas de los avisos por correo apuntan a `127.0.0.1`.** Dentro del panel y en los avisos del navegador las ligas sí abren.
- **Cada navegador es un aparato.** Si abres la liga dentro de otra aplicación y luego en Chrome o Safari, cada uno pide su permiso.
- **En el celular queda como acceso directo, no como aplicación aparte.** El navegador pide el manifiesto de la aplicación sin cookies y la puerta no lo deja pasar. «Agregar a la pantalla de inicio» sí funciona y abre el panel en el navegador.
- **Si el nombre del servidor lo comparte otra aplicación** (mismo nombre, otro puerto), las cookies de ambas viajan a las dos: una falla grave en la otra aplicación puede alcanzar a esta puerta. Con un nombre propio no pasa.
- Para varias personas, cada una con su espacio, usa el modo de varios usuarios del [README](../README.md#dos-modos-de-uso).

## Pruebas

```bash
python -m unittest tests.test_puerta -v
```

Las pruebas de la terminal (el socket de archivo) solo corren en Linux: córrelas también en el servidor, con su Python, antes de instalar. Después de instalar, comprueba con el proxy real que sin permiso nada llega al panel, incluidos los destinos que no son una ruta:

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://facturas.ejemplo.com:8443/api/state                                # 401
curl -s -o /dev/null -w '%{http_code}\n' --request-target 'x:y:/_puerta/' https://facturas.ejemplo.com:8443/        # 400
```
