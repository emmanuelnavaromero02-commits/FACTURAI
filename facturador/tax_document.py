"""Read the recipient's labelled fields from a Mexican Constancia de Situación Fiscal."""
import re
import unicodedata
from datetime import date, datetime
from pathlib import Path

from pypdf import PdfReader

from .local_reader import read_local_page
from .models import Profile


# SAT, catálogo c_RegimenFiscal: https://www.cloudb.sat.gob.mx/datos_fiscales/regimen
REGIME_NAMES = {
    '601': 'General de Ley Personas Morales',
    '603': 'Personas Morales con Fines no Lucrativos',
    '605': 'Sueldos y Salarios e Ingresos Asimilados a Salarios',
    '606': 'Arrendamiento',
    '607': 'Régimen de Enajenación o Adquisición de Bienes',
    '608': 'Demás ingresos',
    '610': 'Residentes en el Extranjero sin Establecimiento Permanente en México',
    '611': 'Ingresos por Dividendos (socios y accionistas)',
    '612': 'Personas Físicas con Actividades Empresariales y Profesionales',
    '614': 'Ingresos por intereses',
    '615': 'Régimen de los ingresos por obtención de premios',
    '616': 'Sin obligaciones fiscales',
    '620': 'Sociedades Cooperativas de Producción que optan por diferir sus ingresos',
    '621': 'Incorporación Fiscal',
    '622': 'Actividades Agrícolas, Ganaderas, Silvícolas y Pesqueras',
    '623': 'Opcional para Grupos de Sociedades',
    '624': 'Coordinados',
    '625': 'Régimen de las Actividades Empresariales con ingresos a través de Plataformas Tecnológicas',
    '626': 'Régimen Simplificado de Confianza',
}


def plain(text):
    return ''.join(c for c in unicodedata.normalize('NFD', text.upper()) if not unicodedata.combining(c))


def one(values):
    unique = set(values)
    return unique.pop() if len(unique) == 1 else None


def matches(text, label, value):
    return re.findall(r'(?<!\w)(?:' + label + r')\s*:\s*(' + value + ')', text, re.I)


def active_regimes(text):
    section = re.search(r'\bREGIMENES\s*:|\bREGIMEN\s+FISCAL\s*:', text)
    if not section:
        return []
    block = re.split(r'\b(?:OBLIGACIONES|ACTIVIDADES ECONOMICAS|CADENA ORIGINAL)\s*:', text[section.end():])[0]
    # Pypdf joins table columns differently across generators. Identify a row by
    # its actual catalog description/code and inspect only the dates belonging to it.
    block = ' '.join(block.split())
    phrases = {re.sub(r'^REGIMEN (?:DE (?:LAS? |LOS? )?)?', '', plain(label)): code
               for code, label in REGIME_NAMES.items()}
    pattern = r'\b(?:' + '|'.join([*REGIME_NAMES, *(re.escape(s) for s in phrases)]) + r')\b'
    rows = []
    for match in re.finditer(pattern, block):
        candidate = match[0]
        code = phrases.get(candidate, candidate)
        if rows and rows[-1][0] == code and not re.search(r'\d{2}/\d{2}/\d{4}', block[rows[-1][1]:match.start()]):
            continue
        rows.append((code, match.start()))
    found = []
    for i, (code, start_at) in enumerate(rows):
        row = block[start_at:rows[i+1][1] if i+1 < len(rows) else None]
        if 'BAJA' in row:
            continue
        dates = re.findall(r'\b\d{2}/\d{2}/\d{4}\b', row)
        if dates:
            try:
                start = datetime.strptime(dates[0], '%d/%m/%Y').date()
                end = datetime.strptime(dates[1], '%d/%m/%Y').date() if len(dates) > 1 else None
            except ValueError:
                continue
            if start > date.today() or (end and end < date.today()):
                continue
        if code not in found:
            found.append(code)
    return found


def parse_constancia(text):
    normalized = ' '.join(plain(text).split())
    if re.search(r'\b(?:COMPROBANTE FISCAL DIGITAL POR INTERNET|TIMBRE FISCAL DIGITAL|RFC (?:DEL )?EMISOR|'
                 r'CERTIFICADO DE (?:LA )?FIRMA ELECTRONICA)\b', normalized):
        raise ValueError('El documento corresponde a otro trámite fiscal. Sube la constancia individual del receptor.')
    if 'CONSTANCIA DE SITUACION FISCAL' not in normalized:
        raise ValueError('Elige la Constancia de Situación Fiscal del receptor emitida por el SAT.')
    upper = text.upper()
    rfcs = matches(upper, r'RFC|REGISTRO FEDERAL DE CONTRIBUYENTES', r'[A-ZÑ&]{3,4}\d{6}[A-Z0-9]{3}\b')
    if len(set(rfcs)) > 1:
        raise ValueError('El archivo contiene más de un contribuyente. Sube una constancia individual.')
    rfc = one(rfcs)
    # Pypdf and OCR may place the next label on the same line.
    name_value = r"[A-ZÁÉÍÓÚÜÑ&][A-ZÁÉÍÓÚÜÑ&.'’ 0-9-]{0,178}?"
    boundary = r'(?=\s*(?:\n|$|(?:PRIMER APELLIDO|SEGUNDO APELLIDO|R[EÉ]GIMEN CAPITAL|NOMBRE COMERCIAL|FECHA DE INICIO|CURP)\s*:))'
    def name_part(label):
        return one(' '.join(v.split()) for v in matches(upper, label, name_value + boundary) if v.strip())

    name = name_part(r'DENOMINACI[ÓO]N/RAZ[ÓO]N SOCIAL|DENOMINACI[ÓO]N O RAZ[ÓO]N SOCIAL|RAZ[ÓO]N SOCIAL')
    if not name:
        first = name_part(r'NOMBRE\s*\(S\)|NOMBRES?')
        surname = name_part('PRIMER APELLIDO')
        second = name_part('SEGUNDO APELLIDO')
        if first and surname:
            name = ' '.join(part for part in (first, surname, second) if part)
    postal = one(matches(upper, r'C[ÓO]DIGO\s*POSTAL(?:\s+FISCAL)?|C\.P\.\s*FISCAL', r'\d{5}\b'))
    email = one(matches(text, r'CORREO ELECTR[ÓO]NICO(?: DEL CONTRIBUYENTE)?', r'[^@\s:]+@[^@\s:]+\.[A-Za-z]{2,}'))
    regimes = active_regimes(plain(text))
    return {'rfc': rfc, 'name': name, 'postal_code': postal, 'email': email,
            'regime': one(regimes), 'regimes': regimes}


def constancia_draft(document: Path, plan=None):
    count = len(PdfReader(document).pages) if document.suffix.lower() == '.pdf' else 1
    if count > 20:
        raise ValueError('Elige la constancia individual de hasta 20 páginas.')
    pages, missed = [], False
    rotation = 0
    for i in range(count):
        try:
            reading = read_local_page(document, i + 1, rotation=rotation)
            if i == 0 and reading['method'] == 'ocr' and 'CONSTANCIA DE SITUACION FISCAL' not in ' '.join(plain(reading['text']).split()):
                for angle in (90, 270, 180):
                    rotated = read_local_page(document, i + 1, rotation=angle)
                    if 'CONSTANCIA DE SITUACION FISCAL' in ' '.join(plain(rotated['text']).split()):
                        reading, rotation = rotated, angle
                        break
            pages.append(reading['text'])
            missed |= not bool(reading['text'].strip())
        except (ValueError, RuntimeError, OSError):
            missed = True
    try:
        draft = parse_constancia('\n'.join(pages))
    except ValueError as exc:
        if not plan or 'más de un contribuyente' in str(exc) or 'otro trámite fiscal' in str(exc):
            raise
        draft = dict(rfc=None, name=None, postal_code=None, email=None, regime=None, regimes=[])
    if missed:
        draft.update(regime=None, regimes=[])
    notes = []
    if plan:
        from .ai import AI
        from .auth import PlanError
        try:
            inferred = AI(plan).tax_profile(document)
        except (PlanError, ValueError, OSError):
            if not any(draft.values()):
                raise ValueError('No se pudo leer la constancia. Sube el PDF original o una imagen más legible.') from None
            notes.append('Se usaron los datos que se pudieron leer en el contenedor.')
        else:
            if not inferred.is_constancia:
                raise ValueError('El archivo no se identificó como constancia fiscal individual del receptor.')
            if draft['rfc'] and inferred.rfc and draft['rfc'] != inferred.rfc.strip().upper():
                raise ValueError('Las lecturas difieren en el RFC. Sube el PDF original de un solo receptor.')
            for field in ('rfc', 'name', 'postal_code', 'email'):
                value = getattr(inferred, field)
                if value and not draft[field]:
                    try:
                        validator = {'rfc': Profile.valid_rfc, 'name': Profile.valid_name,
                                     'postal_code': Profile.valid_postal, 'email': Profile.valid_email}[field]
                        draft[field] = validator(value.strip())
                    except ValueError:
                        pass
            ai_codes = [code for code in inferred.regimes if code in REGIME_NAMES]
            if inferred.regime in REGIME_NAMES:
                ai_codes.append(inferred.regime)
            draft['regimes'] = list(dict.fromkeys([*draft['regimes'], *ai_codes]))
            draft['regime'] = one(draft['regimes']) if not missed or count <= 4 else None
            notes.extend(inferred.uncertainties)
    if len(draft['regimes']) > 1:
        notes.append('Elige el régimen activo que usarás para tus facturas.')
    if missed:
        notes.append('Revisa el régimen: hay páginas que el lector local no pudo leer.')
    unread = [label for field, label in (('rfc', 'RFC'), ('name', 'nombre'), ('postal_code', 'CP fiscal')) if not draft[field]]
    if not draft['regime'] and not draft['regimes']:
        unread.append('régimen fiscal')
    if unread:
        notes.append('No se pudo identificar ' + ', '.join(unread) + ' en la constancia.')
    draft['regime_options'] = [{'code': code, 'name': REGIME_NAMES[code]} for code in draft.pop('regimes')]
    return {**draft, 'uncertainties': list(dict.fromkeys(notes))}
