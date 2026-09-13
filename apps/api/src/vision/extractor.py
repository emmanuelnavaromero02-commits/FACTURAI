import abc
import io
import json
import logging
from typing import Any, Dict, List, Optional
from PIL import Image
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
    async def extract(self, image_bytes: bytes) -> VisionExtractionSchema:
        pass


class FakeVisionExtractor(VisionExtractor):
    """
    Extractor simulado para pruebas unitarias.
    Permite inyectar fixtures predefinidos: 'bueno', 'borroso', 'sin_folio', 'comercio_desconocido'.
    """

    def __init__(self):
        self._fixtures: Dict[str, VisionExtractionSchema] = {}
        self._current_mode: str = "bueno"
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

    async def extract(self, image_bytes: bytes) -> VisionExtractionSchema:
        if self._current_mode in self._fixtures:
            return self._fixtures[self._current_mode]
        return self._fixtures["bueno"]


_extractor_instance: VisionExtractor = FakeVisionExtractor()


def get_vision_extractor() -> VisionExtractor:
    return _extractor_instance


def set_vision_extractor(extractor: VisionExtractor) -> None:
    global _extractor_instance
    _extractor_instance = extractor
