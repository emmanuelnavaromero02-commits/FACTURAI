import abc
import io
import json
import logging
from typing import Any, Dict, List, Optional
from PIL import Image, ImageFilter, ImageOps
from pydantic import BaseModel, Field

try:
    import zxingcpp
except ImportError:
    zxingcpp = None

logger = logging.getLogger(__name__)


class VisionKeyValue(BaseModel):
    etiqueta: str
    valor: str


class VisionExtractionSchema(BaseModel):
    """
    Esquema estricto de salida del modelo de visión.
    Campo que no se aprecie impreso debe ir en null.
    """
    comercio: Optional[str] = None
    url_facturacion: Optional[str] = None
    folio: Optional[str] = None
    web_id: Optional[str] = None
    fecha: Optional[str] = None
    hora: Optional[str] = None
    total: Optional[str] = None
    subtotal: Optional[str] = None
    iva: Optional[str] = None
    sucursal: Optional[str] = None
    caja: Optional[str] = None
    transaccion: Optional[str] = None
    rfc_emisor: Optional[str] = None
    otros: List[VisionKeyValue] = Field(default_factory=list, max_length=5)
    confianza: float = Field(0.0, ge=0.0, le=1.0)


def extract_qr_code(image_bytes: bytes) -> Optional[str]:
    """
    Intenta decodificar un código QR presente en la imagen del ticket.
    Muchos tickets mexicanos contienen en el QR la URL de facturación directa.
    """
    if zxingcpp is None:
        return None

    try:
        image = Image.open(io.BytesIO(image_bytes))
        result = zxingcpp.read_barcode(image)
        if result and result.valid and result.text:
            text_found = result.text.strip()
            # Si el QR contiene una URL o liga HTTP/HTTPS
            if text_found.startswith("http://") or text_found.startswith("https://"):
                return text_found
            return text_found
    except Exception as exc:
        logger.debug("No se pudo leer QR de la imagen: %s", exc)

    return None


class VisionExtractor(abc.ABC):
    """Interfaz abstracta para el cliente del modelo de visión con reintento ante error de parseo."""

    @abc.abstractmethod
    async def extract(
        self,
        image_bytes: bytes,
        model: Optional[str] = None,
    ) -> VisionExtractionSchema:
        pass


class FakeVisionExtractor(VisionExtractor):
    """
    Extractor simulado para pruebas unitarias.
    Permite inyectar fixtures predefinidos: 'bueno', 'borroso', 'sin_folio', 'comercio_desconocido'.
    """

    def __init__(self):
        self._fixtures: Dict[str, VisionExtractionSchema] = {}
        self._current_mode: str = "bueno"
        self.invocations: List[dict] = []
        self._setup_default_fixtures()

    def _setup_default_fixtures(self):
        self._fixtures["bueno"] = VisionExtractionSchema(
            comercio="Oxxo Comercial",
            url_facturacion="https://factura.oxxo.com",
            folio="4821-993-0077",
            web_id="WID-9982",
            fecha="13/09/2026",
            hora="19:42:00",
            total="1,284.50",
            subtotal="1,107.33",
            iva="177.17",
            sucursal="Suc. Río Churubusco",
            caja="02",
            transaccion="TR-55442",
            rfc_emisor="CCO8605231N4",
            otros=[VisionKeyValue(etiqueta="Cajero", valor="Juan P.")],
            confianza=0.95,
        )

        self._fixtures["borroso"] = VisionExtractionSchema(
            comercio="Oxxo Comercial",
            url_facturacion=None,
            folio="4821-???-????",
            total="1,284.50",
            confianza=0.45,  # Confianza < 0.6
        )

        self._fixtures["sin_folio"] = VisionExtractionSchema(
            comercio="Oxxo Comercial",
            url_facturacion="https://factura.oxxo.com",
            folio=None,  # Falta folio
            total="1,284.50",
            confianza=0.90,
        )

        self._fixtures["comercio_desconocido"] = VisionExtractionSchema(
            comercio="Tlapalería Don Pedro Desconocido",
            url_facturacion=None,
            folio="TLAP-0099",
            total="450.00",
            subtotal="387.93",
            iva="62.07",
            rfc_emisor="XXXX010101XXX",
            confianza=0.92,
        )

    def set_mode(self, mode: str):
        self._current_mode = mode

    def register_fixture(self, name: str, schema: VisionExtractionSchema):
        self._fixtures[name] = schema

    async def extract(
        self,
        image_bytes: bytes,
        model: Optional[str] = None,
    ) -> VisionExtractionSchema:
        settings = get_settings()
        self.invocations.append({"model": model, "mode": self._current_mode})
        if self._current_mode == "borroso_escalable":
            if model == settings.ANTHROPIC_MODEL_AGENTE:
                return self._fixtures["bueno"]
            return self._fixtures["borroso"]

        if self._current_mode == "reintento_con_variante":
            matching = [inv for inv in self.invocations if inv.get("mode") == "reintento_con_variante"]
            if len(matching) > 2:
                return self._fixtures["bueno"]
            return self._fixtures["borroso"]

        if self._current_mode in self._fixtures:
            return self._fixtures[self._current_mode]
        return self._fixtures["bueno"]


import base64
import os
import re
import time
import httpx

from ..config import get_model_token_pricing, get_settings

TICKET_SYSTEM_PROMPT = """Eres un especialista de élite en visión computacional y extracción de datos de tickets de compra y facturas en México para facturación electrónica CFDI 4.0.
Tu objetivo es analizar minuciosamente la imagen del ticket de compra y extraer los datos requeridos en formato JSON estricto, incluso si el comprobante está arrugado, doblado, con tinta térmica desvanecida o parcialmente borroso.

Esquema JSON requerido:
{
  "comercio": "Nombre comercial o razón social en el encabezado (ej. OXXO, Walmart, Costco, Soriana, Starbucks, Domino's, Farmacias Guadalajara, gasolinera), o null",
  "url_facturacion": "URL exacta del portal de facturación si viene impresa en el ticket (o en un QR), o null",
  "folio": "Folio del ticket, número de ticket, folio de venta o número de operación indispensable para facturar, o null",
  "web_id": "Código web, Web ID, TC, Trn, Referencia o código alfanumérico complementario requerido para facturar, o null",
  "fecha": "Fecha de compra impresa en formato DD/MM/YYYY o YYYY-MM-DD, o null",
  "hora": "Hora de compra impresa (HH:MM o HH:MM:SS), o null",
  "total": "Importe total numérico pagado (ej. 250.50), o null",
  "subtotal": "Subtotal antes de impuestos si viene desglosado, o null",
  "iva": "Importe de IVA desglosado, o null",
  "sucursal": "Nombre o número de la sucursal/tienda donde se compró, o null",
  "caja": "Número de caja registradora o terminal, o null",
  "transaccion": "Número de transacción/operación/ticket alterno, o null",
  "rfc_emisor": "RFC del comercio emisor (12 o 13 caracteres alfanuméricos), o null si no se aprecia",
  "otros": [
    {"etiqueta": "nombre_del_campo", "valor": "valor_leido"}
  ],
  "confianza": 0.95
}

REGLAS DE EXTRACCIÓN Y RECUPERACIÓN ANTE TICKETS ARRUGADOS O BORROSOS:
1. Responde ÚNICAMENTE con el objeto JSON válido. NO agregues introducciones, explicaciones ni texto fuera del JSON.
2. ETIQUETAS DIVERSAS DE FOLIO EN MÉXICO: Los comercios en México imprimen la clave para facturar bajo muchos nombres:
   "FOLIO", "TICKET", "NO. TICKET", "DOCTO", "TRANSACCIÓN", "TRN", "OPERACIÓN", "ORDEN", "FOLIO DE VENTA", "REF", "ID VENTA", "CÓDIGO DE FACTURACIÓN".
   Si no encuentras la palabra exacta 'Folio', NO lo dejes en null: asigna el número de comprobante o referencia de venta principal al campo 'folio', y códigos complementarios al campo 'web_id' o 'transaccion'.
3. COMPROBANTES TÉRMICOS DESVANECIDOS O ARRUGADOS:
   Examina con sumo detalle las sombras e impresiones tenues causadas por dobleces o baja tinta térmica. Si distingues caracteres con probabilidad razonable, extrae la lectura más coherente.
   Ten en cuenta confusiones típicas de matrices térmicas: '0' con 'O' u '8', '1' con 'I' o 'l', '5' con 'S', '2' con 'Z', '8' con 'B'.
4. VARIABLES Y CAMPOS ADICIONALES EN 'otros':
   Si el comprobante contiene otros números o referencias (ej. "Dígito Verificador", "ID Tienda", "No. Aprobación", "Secuencia", "Referencia"), inclúyelos en 'otros' (hasta 5 pares etiqueta/valor) para que el agente disponga de ellos al interactuar con el portal de facturación.
5. CONFIANZA:
   - 0.85 a 1.0: Ticket nítido con folio, total y comercio claros.
   - 0.60 a 0.84: Ticket algo arrugado, descolorido o con texto tenue, pero donde el total y al menos un número identificador (folio/ticket/web_id) son legibles.
   - < 0.60: Imagen severamente borrosa, cortada o donde ni el total ni ningún número de folio son discernibles con certeza.
6. FORMATO DE IMPORTES: "total", "subtotal" e "iva" deben ser números limpios sin '$' ni comas (ej. "250.50").
7. ESTRICTA PROHIBICIÓN DE ADIVINAR O ALUCINAR DÍGITOS:
   Si un número de folio o monto está parcialmente borroso, cubierto por una arruga o manchado de modo que no puedas distinguir con certeza cada dígito, NO intentes inventar números. Coloca null o caracteres de duda ('?') y asigna una confianza < 0.60. En facturación electrónica mexicana ante el SAT, un dígito malinterpretado invalida la emisión o puede timbrar el consumo de otra persona. Es mil veces preferible reportar la imagen como ilegible para pedir una foto nítida que inventar datos fiscales.
"""


def remove_black_letterbox(img: Image.Image, threshold: int = 20) -> Image.Image:
    """
    Elimina franjas negras externas (letterbox) producidas por capturas de pantalla de celular
    o aplicaciones de cámara, recortando automáticamente al área útil del comprobante.
    """
    try:
        gray = img.convert("L")
        mask = gray.point(lambda p: 255 if p > threshold else 0)
        bbox = mask.getbbox()
        if bbox:
            w, h = img.size
            bw = bbox[2] - bbox[0]
            bh = bbox[3] - bbox[1]
            if (bw * bh) >= 0.35 * (w * h) and (bw < w or bh < h):
                return img.crop(bbox)
    except Exception as exc:
        logger.debug("No se pudo aplicar recorte de franjas negras: %s", exc)
    return img


def clean_camscanner_hd(img: Image.Image) -> Image.Image:
    """
    Filtro HD inteligente estilo CamScanner 'Color Mágico':
    - NO destruye colores ni convierte a escala de grises ruidosa.
    - Preserva intacta la tinta térmica tenue evitando blanqueos agresivos.
    - Aplica autocorrección sutil de contraste y unsharp mask de micro-enfoque.
    """
    from PIL import ImageEnhance

    try:
        img = ImageOps.exif_transpose(img) or img
    except Exception:
        pass

    if img.mode != "RGB":
        img = img.convert("RGB")

    # 1. Quitar franjas negras de letterbox si existen
    img = remove_black_letterbox(img)

    # 2. Ajuste sutil no destructivo de contraste (+12%) para resaltar letras sin borrar tinta tenue
    contrast_enhancer = ImageEnhance.Contrast(img)
    enhanced = contrast_enhancer.enhance(1.12)

    # 3. Elevación suave de brillo (+3%) para nivelar sombras tenues
    brightness_enhancer = ImageEnhance.Brightness(enhanced)
    enhanced = brightness_enhancer.enhance(1.03)

    # 4. Máscara de enfoque micro-HD para definir bordes de números y códigos sin granular
    sharpness_enhancer = ImageEnhance.Sharpness(enhanced)
    enhanced = sharpness_enhancer.enhance(1.25)
    enhanced = enhanced.filter(ImageFilter.UnsharpMask(radius=1.0, percent=50, threshold=3))

    return enhanced


def apply_camscanner_hd_filter(img: Image.Image) -> Image.Image:
    """Compatibilidad: delega al procesador HD no destructivo."""
    return clean_camscanner_hd(img)


def enhance_receipt_image(image_bytes: bytes) -> bytes:
    """
    Mejora visualmente imágenes de tickets térmicos sin pérdida de calidad.
    """
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception:
        pass

    try:
        img = Image.open(io.BytesIO(image_bytes))
        hd_img = clean_camscanner_hd(img)

        buf = io.BytesIO()
        hd_img.save(buf, format="JPEG", quality=92, optimize=True)
        return buf.getvalue()
    except Exception as exc:
        logger.debug("No se pudo aplicar realce CamScanner a la imagen: %s", exc)
        return image_bytes


def convert_image_to_camscanner_pdf(image_bytes: bytes) -> bytes:
    """
    Convierte una imagen de ticket a un PDF escaneado en alta resolución:
    1. Corrige orientación EXIF física.
    2. Elimina franjas negras de captura.
    3. Aplica realce CamScanner no destructivo (preserva colores y textos térmicos).
    4. Genera documento PDF estándar a 150 DPI.
    """
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception:
        pass

    try:
        img = Image.open(io.BytesIO(image_bytes))
        hd_img = clean_camscanner_hd(img)

        buf = io.BytesIO()
        hd_img.save(buf, format="PDF", resolution=150.0)
        return buf.getvalue()
    except Exception as exc:
        logger.warning("Error al generar PDF escaneado CamScanner: %s", exc)
        try:
            img = Image.open(io.BytesIO(image_bytes))
            if img.mode != "RGB":
                img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="PDF", resolution=150.0)
            return buf.getvalue()
        except Exception:
            return image_bytes


def generate_vision_recovery_variants(image_bytes: bytes) -> list[tuple[str, bytes]]:
    """
    Genera variantes visuales orientadas a recuperar la lectura cuando
    la foto original resulta con baja confianza, sin folio o ilegible:
    1. Giro 90° horario (los tickets capturados de lado en horizontal son el fallo #1)
    2. Giro 270° horario (giro antihorario)
    3. Realce selectivo de contraste de tinta térmica
    4. Giro 180° (ticket invertido)
    """
    variants: list[tuple[str, bytes]] = []
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception:
        pass

    try:
        img = Image.open(io.BytesIO(image_bytes))
        img = ImageOps.exif_transpose(img) or img
        if img.mode != "RGB":
            img = img.convert("RGB")
        img = remove_black_letterbox(img)

        w, h = img.size
        is_horizontal = w > (h * 1.15)

        def to_bytes(target: Image.Image, quality: int = 92) -> bytes:
            b = io.BytesIO()
            target.save(b, format="JPEG", quality=quality, optimize=True)
            return b.getvalue()

        # Giros: PIL rotate rota en sentido antihorario; 270 es 90° horario
        rot_90 = img.rotate(270, expand=True)
        rot_270 = img.rotate(90, expand=True)
        rot_180 = img.rotate(180, expand=True)

        from PIL import ImageEnhance
        # Realce selectivo para tinta desvanecida
        ink_enhancer = ImageEnhance.Contrast(img).enhance(1.28)
        ink_enhancer = ImageEnhance.Sharpness(ink_enhancer).enhance(1.35)
        ink_enhancer = ink_enhancer.filter(ImageFilter.UnsharpMask(radius=1.0, percent=65, threshold=2))

        if is_horizontal:
            variants.append(("rotacion_90_grados", to_bytes(rot_90)))
            variants.append(("rotacion_270_grados", to_bytes(rot_270)))
            variants.append(("realce_tinta_termica", to_bytes(ink_enhancer)))
            variants.append(("rotacion_180_grados", to_bytes(rot_180)))
        else:
            variants.append(("realce_tinta_termica", to_bytes(ink_enhancer)))
            variants.append(("rotacion_90_grados", to_bytes(rot_90)))
            variants.append(("rotacion_270_grados", to_bytes(rot_270)))
            variants.append(("rotacion_180_grados", to_bytes(rot_180)))
    except Exception as exc:
        logger.warning("Error al generar variantes de recuperación visual: %s", exc)

    return variants


def prepare_image_for_anthropic(image_bytes: bytes) -> tuple[str, str]:
    """
    Optimiza y convierte la imagen a formato JPEG en base64 para la API de visión.
    Preserva máxima fidelidad de color y microtextos térmicos sin quemar blancos.
    Redimensiona proporcionalmente a máximo 1568px en el lado mayor.
    """
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception:
        pass

    try:
        img = Image.open(io.BytesIO(image_bytes))
        img = ImageOps.exif_transpose(img) or img
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")

        # Quitar franjas negras de encuadre si las hay
        img = remove_black_letterbox(img)

        # Aplicar realce suave
        img = clean_camscanner_hd(img)

        # Limitar a máximo 1568px para velocidad y precisión óptima de Claude Vision
        max_dim = 1568
        if max(img.size) > max_dim:
            scale = max_dim / max(img.size)
            new_size = (int(img.size[0] * scale), int(img.size[1] * scale))
            img = img.resize(new_size, Image.Resampling.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90, optimize=True)
        out_bytes = buf.getvalue()
        media_type = "image/jpeg"
    except Exception:
        out_bytes = image_bytes
        media_type = "image/jpeg"

    return base64.b64encode(out_bytes).decode("utf-8"), media_type


class AnthropicVisionExtractor(VisionExtractor):
    """
    Extractor de visión real utilizando la Messages API de Anthropic.
    El modelo por defecto proviene de ANTHROPIC_MODEL_VISION en config.py.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
    ):
        settings = get_settings()
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY") or settings.ANTHROPIC_API_KEY
        self.model = model or os.environ.get("ANTHROPIC_MODEL_VISION") or settings.ANTHROPIC_MODEL_VISION

    async def extract_with_usage(
        self,
        image_bytes: bytes,
        model: Optional[str] = None,
    ) -> tuple[VisionExtractionSchema, Dict[str, Any]]:
        """
        Ejecuta la llamada a la API de Anthropic y retorna el esquema extraído
        junto con metadatos de tokens consumidos, costo estimado y tiempo.
        """
        if not self.api_key:
            raise ValueError(
                "No se encontró la clave de API de Anthropic (ANTHROPIC_API_KEY). "
                "Configura la variable de entorno o pásala como parámetro."
            )

        active_model = model or self.model
        b64_image, media_type = prepare_image_for_anthropic(image_bytes)

        payload = {
            "model": active_model,
            "max_tokens": 1500,
            "system": TICKET_SYSTEM_PROMPT,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": b64_image,
                            },
                        },
                        {
                            "type": "text",
                            "text": "Extrae los datos de este ticket de compra mexicano en formato JSON según el esquema solicitado.",
                        },
                    ],
                }
            ],
        }

        start_time = time.time()
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=payload,
            )

        elapsed = time.time() - start_time

        if response.status_code != 200:
            err_text = response.text
            raise RuntimeError(
                f"Error de la API de Anthropic ({response.status_code}): {err_text}"
            )

        resp_data = response.json()
        usage = resp_data.get("usage", {})
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        total_tokens = input_tokens + output_tokens

        # Cálculo de costo en USD: únicamente si ambas tarifas están configuradas
        rates = get_model_token_pricing(active_model)
        if rates is not None:
            cost_usd: Optional[float] = (input_tokens * rates["input"] / 1_000_000) + (
                output_tokens * rates["output"] / 1_000_000
            )
        else:
            cost_usd = None

        content_list = resp_data.get("content", [])
        text_response = "".join(b.get("text", "") for b in content_list if b.get("type") == "text").strip()
        if not text_response:
            raise ValueError("Respuesta vacía o inesperada de la API de Anthropic.")

        raw_text = text_response
        if "```json" in raw_text:
            raw_text = raw_text.split("```json", 1)[1].split("```", 1)[0].strip()
        elif "```" in raw_text:
            raw_text = raw_text.split("```", 1)[1].split("```", 1)[0].strip()
        elif "{" in raw_text and "}" in raw_text:
            start_idx = raw_text.find("{")
            end_idx = raw_text.rfind("}") + 1
            raw_text = raw_text[start_idx:end_idx]

        # Limpieza robusta de JSON (comentarios y comas finales huérfanas producidas por LLMs)
        cleaned_json = re.sub(r"//.*?\n", "\n", raw_text)
        cleaned_json = re.sub(r",\s*([}\]])", r"\1", cleaned_json)

        try:
            parsed_json = json.loads(cleaned_json)
        except Exception:
            parsed_json = json.loads(raw_text)

        # Coerción de tipos para robustez contra respuestas numéricas del LLM (ej. float/int a str)
        if isinstance(parsed_json, dict):
            for field in ("total", "subtotal", "iva", "folio", "web_id", "caja", "transaccion", "rfc_emisor", "comercio", "fecha", "hora", "sucursal", "url_facturacion"):
                if field in parsed_json and parsed_json[field] is not None:
                    parsed_json[field] = str(parsed_json[field]).strip()

        schema = VisionExtractionSchema.model_validate(parsed_json)

        meta = {
            "model": active_model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "cost_usd": cost_usd,
            "elapsed_seconds": round(elapsed, 2),
            "media_type": media_type,
            "image_size_bytes": len(image_bytes),
        }

        return schema, meta

    async def extract(
        self,
        image_bytes: bytes,
        model: Optional[str] = None,
    ) -> VisionExtractionSchema:
        schema, _ = await self.extract_with_usage(image_bytes, model=model)
        return schema


_extractor_instance: VisionExtractor = FakeVisionExtractor()


def get_vision_extractor(model: Optional[str] = None) -> VisionExtractor:
    global _extractor_instance
    settings = get_settings()
    if isinstance(_extractor_instance, FakeVisionExtractor) and settings.ENVIRONMENT not in ("test", "testing") and settings.ANTHROPIC_API_KEY:
        return AnthropicVisionExtractor(model=model)
    return _extractor_instance


def set_vision_extractor(extractor: VisionExtractor) -> None:
    global _extractor_instance
    _extractor_instance = extractor
